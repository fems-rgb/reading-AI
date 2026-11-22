"""单页链路：质量门 → A1‖A2 → A3 三层摘要 → A4 提问 → 自检闸门 → 落库。

这一层不持有会话状态，只做"纯处理"，方便单测和离线评测集跑批。
会话状态机在 orchestrator.py。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .. import db
from ..config import get_settings
from ..utils import age_band, now, split_sentences, truncate
from ..services import nlg, speech
from ..services.brain import Brain
from ..services.metrics import Tracer
from ..services.selfcheck import SelfChecker
from ..services.vision import OcrService, VlmService, assess_quality

_checker = SelfChecker()


@dataclass
class PageRequest:
    session_id: str
    page_no: int
    child: dict
    book: dict
    library_page: dict | None
    image_bytes: bytes
    history: list[dict]


def _qtype_from_bank(item: dict) -> str:
    return item.get("qtype") if item.get("qtype") in nlg.TYPE_LABELS else "observe"


def _pick_bank(page: dict | None, child: dict, history: list[dict], prefer: str) -> dict | None:
    if not page:
        return None
    items = db.list_bank(book_page_id=page["id"])
    age = int(child.get("age_months") or 48)
    items = [
        item for item in items
        if int(item["age_min_months"]) <= age <= int(item["age_max_months"])
    ]
    if not items:
        return None
    recent = [truncate(str(turn.get("question") or ""), 20) for turn in history[-3:]]
    ordered = sorted(items, key=lambda item: 0 if item["qtype"] == prefer else 1)
    for item in ordered:
        if all(item["text"] != text for text in recent):
            return item
    return None


def _vlm_saw_something(vlm) -> bool:
    """本次 VLM 是否真的看到了东西：降级或四个字段全空都算没看到。"""
    return not vlm.degraded and bool(vlm.characters or vlm.objects or vlm.scene or vlm.relations)


def _facts_from(ocr, vlm, page: dict | None) -> dict:
    # 画面事实只能有一个来源：本次实拍看到了就全用实拍，否则整体回退到
    # 已校对（status=ready）的库页缓存。绝不逐字段混拼——混拼会让讲稿来自
    # 实拍、题目来自旧库页，线上就出现过“人躺在床上”却问“小猫在做什么”。
    live = _vlm_saw_something(vlm)
    characters = list(vlm.characters or []) if live else list((page or {}).get("characters") or [])
    objects = list(vlm.objects or []) if live else list((page or {}).get("objects") or [])
    relations = list(vlm.relations or []) if live else list((page or {}).get("relations") or [])
    scene = (vlm.scene or "") if live else str((page or {}).get("scene") or "")
    keywords = (characters + objects)[:5] if live else list((page or {}).get("keywords") or [])
    if not keywords:
        keywords = (characters + objects)[:5]
    raw_text = ocr.text or (page or {}).get("raw_text", "") or ""
    if ocr.confidence and ocr.confidence < 0.6 and raw_text:
        raw_text = raw_text + "\n[识别不确定]"
    return {
        "raw_text": raw_text,
        "characters": characters,
        "objects": objects,
        "relations": relations,
        "scene": scene,
        "mood": (vlm.mood or "") if live else str((page or {}).get("mood") or ""),
        "keywords": keywords,
        "ocr_confidence": round(float(ocr.confidence or 0), 3),
        "ocr_mode": ocr.mode,
        "vlm_mode": vlm.mode,
        "facts_source": "live" if live else ("library" if page else "empty"),
        "degraded": bool(ocr.degraded or vlm.degraded),
    }


async def analyze_page(request: PageRequest, tracer: Tracer | None = None) -> dict:
    tracer = tracer or Tracer(request.session_id, request.page_no)
    try:
        return await _analyze_page(request, tracer)
    finally:
        # 埋点缓在内存，出口批量写一次；失败也不影响本次结果
        await tracer.aflush()


async def _analyze_page(request: PageRequest, tracer: Tracer) -> dict:
    settings = get_settings()
    started = time.perf_counter()
    child, book, page = request.child, request.book, request.library_page
    age = int(child.get("age_months") or 48)
    language = (book.get("language") or "zh").lower()

    # 0. 拍糊了就不进识别链路（规范 §5.1 失败兜底）；PIL 解码放线程，不阻塞事件循环
    quality_started = time.perf_counter()
    quality = await asyncio.to_thread(assess_quality, request.image_bytes)
    tracer.record(
        "A0_quality",
        (time.perf_counter() - quality_started) * 1000,
        provider="local",
        model="pil",
        ok=quality.ok,
        error=quality.code,
    )
    if not quality.ok:
        return {
            "ok": False,
            "page_no": request.page_no,
            "quality": quality.to_dict(),
            "human_hint": quality.hint,
            "actions": quality.actions,
            "state_label": "识别中",
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    # 1. A1 ‖ A2 并行
    ocr_service, vlm_service = OcrService(), VlmService()
    ocr_slot = time.perf_counter()
    ocr_result, vlm_result = await asyncio.gather(
        asyncio.to_thread(ocr_service.read_sync, request.image_bytes, page),
        asyncio.to_thread(vlm_service.analyze_sync, request.image_bytes, page),
    )
    elapsed = (time.perf_counter() - ocr_slot) * 1000
    tracer.record("A1_ocr", elapsed, provider=ocr_result.mode, model="ocr", ok=not ocr_result.degraded)
    tracer.record("A2_vlm", elapsed, provider=vlm_result.mode, model="vlm", ok=not vlm_result.degraded)

    facts = _facts_from(ocr_result, vlm_result, page)
    brain = Brain(tracer)

    # 2. A3 三层摘要 + 自检
    fuse_started = time.perf_counter()
    draft = await brain.fuse(facts, age, language)
    tracer.record(
        "A3_summary",
        (time.perf_counter() - fuse_started) * 1000,
        provider=draft.get("mode", "rule"),
        model="summary",
    )
    summary_target = f"{request.session_id}:{request.page_no}:summary"

    async def regen_summary(actions: list[dict]) -> dict:
        avoid: list[str] = []
        for action in actions:
            avoid.extend(str(item) for item in action.get("avoid") or [])
        return await brain.fuse(facts, age, language, avoid=avoid)

    gate_started = time.perf_counter()
    summary, summary_gate = await _checker.gate_summary(
        draft,
        facts,
        age_months=age,
        language=language,
        regenerate=regen_summary,
        target=summary_target,
    )
    tracer.record(
        "A11_summary",
        (time.perf_counter() - gate_started) * 1000,
        provider="rules",
        model="selfcheck",
        ok=summary_gate["verdict"] != "block",
        error=summary_gate["verdict"],
    )

    # 3. 题型：题库优先，安静型页面才现生成
    ask_started = time.perf_counter()
    prefer = nlg.choose_type(facts, request.history, age)
    bank_item = _pick_bank(page, child, request.history, prefer)
    if bank_item:
        qtype = _qtype_from_bank(bank_item)
        initial_question = {
            "question": bank_item["text"],
            "type": qtype,
            "expected": (facts.get("objects") or facts.get("characters") or [""])[0],
            "hint": "来自题库",
            "choices": [item for item in (facts.get("characters") or []) + (facts.get("objects") or [])][:3],
            "language": language,
            "mode": "bank",
        }
        source = "bank"
    else:
        qtype = prefer
        initial_question = await brain.question(
            facts, summary, age, language, qtype, request.history
        )
        source = "agent"

    tracer.record(
        "A4_ask",
        (time.perf_counter() - ask_started) * 1000,
        provider=source,
        model="question",
    )

    question_target = f"{request.session_id}:{request.page_no}:question"

    async def regen_question(avoid: list[str], need_different: bool) -> dict:
        next_type = qtype
        if need_different:
            order = ["observe", "guess", "retell", "empathy", "life"]
            next_type = order[(order.index(qtype) + 1) % len(order)] if qtype in order else "observe"
        return await brain.question(
            facts, summary, age, language, next_type, request.history, avoid=avoid
        )

    gate_started = time.perf_counter()
    final_question, question_gate = await _checker.gate_question(
        initial_question,
        facts,
        age_months=age,
        language=language,
        history=request.history,
        regenerate=regen_question,
        target=question_target,
    )
    tracer.record(
        "A11_question",
        (time.perf_counter() - gate_started) * 1000,
        provider="rules",
        model="selfcheck",
        ok=question_gate["verdict"] != "block",
        error=question_gate["verdict"],
    )
    if final_question.get("mode") == "safe_template":
        source = "safe_template"

    # 4. 落库（只存短文本，不存图片/音频）；耗时先算好随行写入
    latency = round((time.perf_counter() - started) * 1000)
    turn_id = db.add_turn(
        request.session_id,
        request.page_no,
        (page or {}).get("id"),
        final_question["question"],
        final_question.get("type") or qtype,
        {
            "verdict": question_gate["verdict"],
            "score": question_gate["score"],
            "summary": question_gate["summary"],
            "checks": question_gate["checks"],
            "summary_gate": {
                "verdict": summary_gate["verdict"],
                "score": summary_gate["score"],
                "summary": summary_gate["summary"],
            },
            "source": source,
            "expected": final_question.get("expected", ""),
            "band": age_band(age),
            "checked_at": now(),
        },
        source=source,
        latency_ms=latency,
    )

    tracer.record("page_total", latency, provider="pipeline", model="")
    return {
        "ok": True,
        "turn_id": turn_id,
        "page_no": request.page_no,
        "book_page_id": (page or {}).get("id"),
        "mode": "library" if page and page.get("status") == "ready" else "live",
        "quality": quality.to_dict(),
        "facts": facts,
        "summary": summary,
        "question": final_question,
        "source": source,
        "checker": {
            "question": question_gate,
            "summary": summary_gate,
        },
        "latency_ms": latency,
        "state_label": "正在提问",
        "display": {
            "title": f"第 {request.page_no} 页",
            "description": summary.get("faithful_description", ""),
            "script": summary.get("read_aloud_script", ""),
            "raw_text": summary.get("raw_text", ""),
            "keywords": summary.get("keywords", []),
        },
    }


async def handle_answer(
    *,
    session: dict,
    child: dict,
    book: dict,
    page_no: int,
    turn: dict,
    facts: dict,
    answer_text: str = "",
    audio: bytes = b"",
    tracer: Tracer | None = None,
) -> dict:
    settings = get_settings()
    tracer = tracer or Tracer(session["id"], page_no)
    language = (book.get("language") or "zh").lower()
    age = int(child.get("age_months") or 48)

    asr = {"text": answer_text, "confidence": 1.0 if answer_text else 0.0, "mode": "text"}
    if not answer_text and audio:
        started = time.perf_counter()
        asr = await asyncio.to_thread(speech.transcribe, audio, language)
        tracer.record(
            "A6_asr",
            (time.perf_counter() - started) * 1000,
            provider=asr.get("mode", ""),
            model="asr",
            ok=bool(asr.get("text")),
        )

    text = (asr.get("text") or "").strip()
    confidence = float(asr.get("confidence") or 0)
    brain = Brain(tracer)

    # 低置信度不猜：直接走确认式追问（规范 §5.3）
    if text and confidence and confidence < settings.asr_confirm_threshold:
        question = turn.get("question") or ""
        understanding = {
            "understood": "partial",
            "action": "confirm",
            "reply": f"你是不是说「{truncate(text, 8)}」？",
            "confidence": confidence,
            "mode": "guard",
        }
        if any(word in text for word in ("不知道", "不会", "不懂")):
            understanding = await brain.understand(facts, question, text, age, confidence)
    else:
        understanding = await brain.understand(
            facts, turn.get("question") or "", text, age, confidence
        )

    reply_audio = await asyncio.to_thread(speech.synthesize, understanding["reply"], "")
    db.update_turn(
        turn["id"],
        answer_text=text,
        asr_confidence=round(confidence, 3),
        understood=understanding["understood"],
        action=understanding["action"],
        reply=understanding["reply"],
    )
    await tracer.aflush()
    return {
        "ok": True,
        "answer_text": text,
        "asr": asr,
        "understood": understanding["understood"],
        "action": understanding["action"],
        "reply": understanding["reply"],
        "audio": reply_audio,
        "mode": understanding.get("mode", "rule"),
        "state_label": "在听你说",
    }


FREE_SYSTEM = (
    "你是幼儿园绘本共读机器人小读，正在陪 3-6 岁孩子读绘本。"
    "用 1-2 句、每句不超过 15 个字、孩子听得懂的简体中文回答。"
    "只能依据给这一页的内容回答；页里没有的细节不编，可以邀请孩子一起看看图画或翻到下一页。"
    "不出现英文、拼音、编号、markdown、表情符号；不讲恐怖、暴力、成人内容。"
    '只输出一个 JSON 对象：{"answer": "……"}。'
)

# 孩子会听到的话，这几类一律不进回调
_FREE_UNSAFE = ("死", "血", "杀", "尸", "鬼", "恐怖", "暴力", "自杀", "裸", "性器官")


def _clean_free_answer(text: str) -> str:
    """自由回答的出口闸门：去格式、控长度、拦不适龄词。不合格就返回空串走安全模板。"""
    import re

    cleaned = re.sub(r"```[a-zA-Z]*", "", text or "").replace("`", "").strip()
    cleaned = cleaned.strip("\"'“”‘’ ")
    if not cleaned:
        return ""
    if any(word in cleaned for word in _FREE_UNSAFE):
        return ""
    if len(re.findall(r"[A-Za-z]{3,}", cleaned)) >= 2:  # 两句英文就该换中文模板
        return ""
    sentences = split_sentences(cleaned)[:2]
    answer = "".join(sentences) if sentences else cleaned
    return truncate(answer, 60)


async def answer_free(
    *,
    child: dict,
    book: dict,
    page_no: int,
    script: str,
    question: str,
    tracer: Tracer | None = None,
    timeout: float = 25.0,
) -> dict:
    """孩子自由提问：带当前页讲稿做上下文，短句回答；模型不可用时用安全模板。"""
    from ..services.llm import LocalLLM

    started = time.perf_counter()
    question = (question or "").strip()
    age = int(child.get("age_months") or 48)
    if not question:
        return {"ok": False, "question": "", "answer": "我没听清，再说一遍好吗？", "mode": "guard", "latency_ms": 0}

    answer, mode = "", "rule"
    llm = LocalLLM()
    if llm.enabled:
        user = (
            f"绘本：《{book.get('title') or ''}》，第 {page_no} 页。\n"
            f"这一页讲的是：{script or '（这一页还没讲，不要猜内容）'}\n"
            f"孩子（{age} 个月）问：{question}\n"
            "请按系统要求只输出 JSON。"
        )
        try:
            data, _meta = await asyncio.wait_for(
                asyncio.to_thread(llm.chat_json, FREE_SYSTEM, user, 0.5), timeout=timeout
            )
            answer = _clean_free_answer(str((data or {}).get("answer") or ""))
            mode = "llm" if answer else "gate"
        except Exception:  # 模型超时/没启用都不得让孩子干等
            answer = ""
    if not answer:
        answer = "这个问题问得真好，我们再看看这一页，一起找找答案。" if "为什么" in question else "我们一起看看图画里有什么，好吗？"
        mode = "rule"

    latency = (time.perf_counter() - started) * 1000
    if tracer:
        tracer.record("free_qa", latency, provider=mode, model="llm")
    return {"ok": True, "question": question, "answer": answer, "mode": mode, "latency_ms": round(latency)}


async def generate_advice(session: dict, child: dict, turns: list[dict], tracer: Tracer | None = None) -> tuple[list[dict], dict]:
    """A10 + 自检：无依据的建议直接丢弃，不写进库。"""
    tracer = tracer or Tracer(session["id"], 0)
    brain = Brain(tracer)
    items = await brain.advise(child, turns)
    items = [item for item in items if item.get("evidence")][:5]
    report = _checker.check_advice(items, turns, child)
    if report.verdict != "pass":
        # 只保留通过检查的字段：去掉无依据项后重新检查一次
        items = [item for item in items if item.get("evidence")]
        report = _checker.check_advice(items, turns, child)
    saved = []
    for item in items:
        advice_id = db.add_advice(
            child["id"], session["id"], item["text"], item["category"], item["evidence"]
        )
        saved.append({**item, "id": advice_id, "status": "pending"})
    result = report.to_dict()
    result["saved"] = len(saved)
    db.add_selfcheck("content", f"{session['id']}:advice", report.verdict, report.score, result["checks"], [])
    await tracer.aflush()
    return saved, result


def build_weekly_report(child: dict, turns: list[dict], advice_items: list[dict]) -> tuple[dict, dict]:
    """只做聚合，不重新读原图/原音频（规范 §5.7）。"""
    from datetime import datetime, timedelta

    week_end = datetime.now()
    week_start = week_end - timedelta(days=7)
    answered = [turn for turn in turns if (turn.get("answer_text") or "").strip()]
    questions = [turn.get("question") for turn in turns if turn.get("question")]
    highlight_turns = [turn for turn in answered if turn.get("understood") == "yes"][:3]
    highlights = [
        f"「{truncate(turn.get('question') or '', 16)}」— 孩子说：{truncate(turn.get('answer_text') or '', 16)}"
        for turn in highlight_turns
    ]
    note = ""
    accepted = [item for item in advice_items if item.get("status") == "accepted"]
    if accepted:
        note = truncate(accepted[0].get("edited_text") or accepted[0].get("text") or "", 80)
    payload = {
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": week_end.strftime("%Y-%m-%d"),
        "headline": f"{child['name']} 这周一起读了 {len(questions)} 页绘本",
        "summary": (
            f"共读 {len(questions)} 页，其中 {len(answered)} 页有回应。"
            if questions else "这周还没有共读记录，期待下次一起读绘本。"
        ),
        "highlights": highlights,
        "teacher_note": note,
        "suggest_home": "在家可以问孩子：今天书里谁最有趣？",
    }
    report = _checker.check_report(payload, child)
    return payload, report.to_dict()


def split_low_confidence(text: str) -> list[str]:
    return split_sentences(text)