"""画面事实接地：来源不混拼、未校对页不用、LLM 编造实体回退规则题。

背景（真实现场）：空白绘本页上传时视觉模型编出“小猫在草地上奔跑”，被标成
needs_review 后仍进了会话；实拍画面是“人躺在床上”，A4 却照着旧库页问
“小猫在做什么？”。这里把三条防线钉死。
"""

from __future__ import annotations

import asyncio

from app import db, lexicon
from app.core.orchestrator import SessionRuntime, pick_ready_page
from app.core.pipeline import _facts_from, _vlm_saw_something
from app.services import nlg, selfcheck, vision
from app.services.brain import Brain, _ungrounded_entities

db.init_db()


def _ocr(text: str = "", confidence: float = 0.0) -> vision.OcrResult:
    return vision.OcrResult(text=text, lines=[], confidence=confidence, mode="stub", degraded=False)


def _vlm(**kwargs) -> vision.VlmResult:
    fields = {
        "characters": [],
        "objects": [],
        "scene": "",
        "relations": [],
        "mood": "",
        "mode": "vlm:test",
        "degraded": False,
    }
    fields.update(kwargs)
    return vision.VlmResult(**fields)


PAGE = {
    "id": 1,
    "page_no": 1,
    "status": "ready",
    "raw_text": "小猫在草地上。",
    "characters": ["小猫"],
    "objects": ["草地", "球"],
    "relations": ["小猫在草地上奔跑"],
    "scene": "草地",
    "keywords": ["小猫", "草地"],
}


# ---------- 1. 事实来源只能有一个 ----------


def test_live_vlm_wins_and_does_not_mix_library_facts():
    facts = _facts_from(_ocr(), _vlm(objects=["书本", "床"], scene="卧室", relations=["躺在床上"]), PAGE)
    assert facts["facts_source"] == "live"
    assert facts["objects"] == ["书本", "床"]
    assert facts["characters"] == [], "库页里的「小猫」不能混进实拍事实"
    assert facts["scene"] == "卧室"
    assert "小猫" not in facts["keywords"]


def test_degraded_vlm_falls_back_to_ready_page_as_a_whole():
    facts = _facts_from(_ocr(), _vlm(objects=["书本"], degraded=True), PAGE)
    assert facts["facts_source"] == "library"
    assert facts["characters"] == ["小猫"]
    assert facts["objects"] == ["草地", "球"]
    assert facts["relations"] == ["小猫在草地上奔跑"]


def test_empty_vlm_without_page_is_empty_not_guessed():
    facts = _facts_from(_ocr(), _vlm(), None)
    assert facts["facts_source"] == "empty"
    assert facts["characters"] == [] and facts["objects"] == []
    assert facts["keywords"] == []


def test_vlm_saw_something_requires_content_and_no_degraded():
    assert _vlm_saw_something(_vlm(scene="卧室")) is True
    assert _vlm_saw_something(_vlm()) is False
    assert _vlm_saw_something(_vlm(scene="卧室", degraded=True)) is False


# ---------- 2. 未校对页不给孩子用 ----------


def test_pick_ready_page_skips_needs_review():
    pages = [
        {"page_no": 1, "status": "needs_review", "objects": ["一只小猫在草地上奔跑"]},
        {"page_no": 2, "status": "ready", "objects": ["风筝"]},
    ]
    assert pick_ready_page(pages, 1) is None, "模型幻觉页未校对，不能当库页用"
    assert pick_ready_page(pages, 2) == pages[1]


def test_start_refuses_book_without_ready_page():
    runtime = SessionRuntime(
        session={"id": "test-no-ready"},
        child={"id": 1, "name": "测试", "age_months": 60},
        book={"id": 1, "title": "测试绘本", "language": "zh"},
        pages=[{"page_no": 1, "status": "needs_review"}],
    )
    result = asyncio.run(runtime.start())
    assert result["ok"] is False
    assert "校对" in result["human_hint"]


def test_start_allows_book_with_ready_page():
    runtime = SessionRuntime(
        session={"id": "test-has-ready"},
        child={"id": 1, "name": "测试", "age_months": 60},
        book={"id": 1, "title": "测试绘本", "language": "zh"},
        pages=[{"page_no": 1, "status": "ready"}],
    )
    assert asyncio.run(runtime.start())["ok"] is True


# ---------- 3. LLM 编造实体必须回退规则题 ----------


class _FakeLLM:
    enabled = True

    def __init__(self, question: str) -> None:
        self.question = question

    def chat_json(self, system: str, user: str, **kwargs):
        return (
            {"question": self.question, "type": "observe", "expected": "书本", "hint": "看看"},
            {"tokens_in": 1, "tokens_out": 1, "model": "fake"},
        )


DESK_FACTS = {"characters": [], "objects": ["书本", "床"], "relations": ["躺在床上"], "scene": "卧室", "keywords": ["书本"]}
SUMMARY = {"faithful_description": "人在看书。", "read_aloud_script": "看书。", "keywords": ["书本"], "band": "4_6"}


def _ask(question: str) -> dict:
    brain = Brain()
    brain.llm = _FakeLLM(question)
    return asyncio.run(brain.question(DESK_FACTS, SUMMARY, 60, "zh", "observe", []))


def test_hallucinated_question_falls_back_to_rule():
    result = _ask("小猫在做什么？")
    assert result["mode"] == "rule"
    assert result["degraded_reason"] == "ungrounded_question"
    assert result["ungrounded"] == ["小猫"]
    assert "小猫" not in result["question"]


def test_grounded_question_keeps_llm_output():
    result = _ask("床上有什么呀？")
    assert result["mode"] == "llm"
    assert result["question"] == "床上有什么呀？"


def test_ungrounded_entities_ignores_meta_words():
    assert _ungrounded_entities("画面里有什么呀？", DESK_FACTS) == []
    assert _ungrounded_entities("小猫在做什么？", DESK_FACTS) == ["小猫"]


def test_rule_question_passes_gate_after_meta_word_is_stripped():
    report = selfcheck.ContentChecker().check_question(
        {"question": "画面里有什么呀？", "type": "observe"},
        DESK_FACTS,
        age_months=60,
        language="zh",
        history=[],
    )
    assert report.verdict == "pass", report.to_dict()


# ---------- 4. 入口过滤与词表 ----------


def test_short_labels_drops_sentences():
    kept = vision._short_labels(["一只小猫在草地上奔跑", "床", "书本。", "", "床头柜"])
    assert kept == ["床", "床头柜"]


def test_lexicon_covers_living_scene_nouns():
    found = set(lexicon.find_nouns("人躺在床上，旁边是床头柜和手机，卧室里开着电视"))
    assert {"人", "床", "床头柜", "手机", "卧室", "电视"} <= found


# ---------- 5. A7 回复话术由规则生成（小模型会把问句/答案说反） ----------


def test_reply_for_give_answer_does_not_repeat_wrong_answer():
    reply = nlg.reply_for("give_answer", DESK_FACTS, "手机")
    assert "手机" not in reply, reply
    assert "书本" in reply or "看看" in reply, reply


def test_reply_for_encourage_mentions_child_entity():
    assert "书" in nlg.reply_for("encourage", DESK_FACTS, "书")


def test_reply_for_confirm_quotes_child_words():
    assert "书" in nlg.reply_for("confirm", DESK_FACTS, "书")
