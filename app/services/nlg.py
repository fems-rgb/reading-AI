"""确定性文本生成（stub 模式，也是 LLM 失败时的降级路径）。

为什么不全交给大模型：
- A3 三层摘要必须"不编造"，模板化转述比自由生成更可控；
- A4 一次只问一个问题、限字数、限题型，规则比提示词更硬；
- 离线园区 GPU 并发有限，模板路径零排队、可复现、可评测。
大模型在 http 模式接管，但输出仍要过 A11 自检。
"""

from __future__ import annotations

from .. import lexicon
from ..utils import age_band, normalize, split_sentences, truncate

TYPE_LABELS = {
    "observe": "观察型",
    "guess": "猜想型",
    "empathy": "共情型",
    "life": "生活联结",
    "retell": "复述型",
}

MAX_LEN = {"under3": 8, "3_4": 12, "4_6": 20}

_ONOMATOPOEIA = {
    "兔子": "蹦蹦跳", "小兔子": "蹦蹦跳", "小狗": "汪汪叫", "小猫": "喵喵叫",
    "小鸟": "叽叽喳", "小鸭": "嘎嘎叫", "青蛙": "呱呱叫", "小熊": "慢慢走",
    "汽车": "嘟嘟嘟", "火车": "轰隆隆", "雨": "滴答滴", "风": "呼呼吹",
}


def _clip_sentence(text: str, limit: int = 15) -> str:
    text = (text or "").strip().strip("。")
    if len(text) <= limit:
        return text
    return text[:limit]


def fuse(facts: dict, age_months: int, language: str = "zh") -> dict:
    """A3：三层摘要。只重组 A1/A2 的事实，不新增任何内容。"""
    raw_text = (facts.get("raw_text") or "").strip()
    characters = [c for c in (facts.get("characters") or []) if c]
    objects = [o for o in (facts.get("objects") or []) if o]
    relations = [r for r in (facts.get("relations") or []) if r]
    scene = (facts.get("scene") or "").strip()
    keywords = list(facts.get("keywords") or [])
    band = age_band(age_months)

    sentences: list[str] = []
    if characters:
        sentences.append(_clip_sentence("画面里有" + "、".join(characters[:2])))
    if relations:
        sentences.append(_clip_sentence(relations[0]))
    elif objects:
        sentences.append(_clip_sentence("还有" + "、".join(objects[:3])))
    if scene and len(sentences) < 3:
        sentences.append(_clip_sentence(scene))
    if not sentences:
        sentences.append("这一页有图画" if language != "en" else "There is a picture")

    if language == "en":
        english_bits = [f"{' and '.join(characters[:2])}" if characters else "someone"]
        if objects:
            english_bits.append(", ".join(objects[:2]))
        faithful = f"You can see {english_bits[0]}."
        if len(english_bits) > 1:
            faithful += f" There is {english_bits[1]}."
        script = faithful
    else:
        faithful = "。".join(sentences) + "。"
        if band == "under3":
            head = characters[0] if characters else "小朋友"
            sound = _ONOMATOPOEIA.get(head, "看一看")
            script = f"{head}，{sound}。"
        else:
            script = "看呀，" + "，".join(sentences[:3]) + "。"
        script = truncate(script, 120)

    if not keywords:
        keywords = (characters + objects)[:5]

    return {
        "raw_text": raw_text,
        "faithful_description": faithful,
        "read_aloud_script": script,
        "speculative_flags": [],
        "keywords": keywords,
        "language": language,
        "band": band,
    }


def choose_type(facts: dict, history: list[dict], age_months: int) -> str:
    """按页型 + 历史配额选题型：每 3 题最多 1 道封闭式，避免孩子只会答"对"。"""
    recent = [turn for turn in history[-3:]]
    closed_recent = sum(1 for turn in recent if turn.get("closed"))
    has_character = bool(facts.get("characters"))
    has_object = bool(facts.get("objects"))
    if age_months < 36:
        return "observe"
    if closed_recent >= 1:
        return "observe" if has_object else "retell"
    if age_months < 48:
        return "observe" if has_object else ("retell" if has_character else "observe")
    if history and len(history) % 3 == 2:
        return "empathy"
    if has_object:
        return "observe"
    return "guess"


def question(
    facts: dict,
    summary: dict,
    age_months: int,
    language: str,
    qtype: str,
    history: list[dict],
    avoid: list[str] | None = None,
) -> dict:
    """A4 规则版：句子短、只用画面事实、带图标兜底选项。"""
    band = age_band(age_months)
    characters = facts.get("characters") or []
    objects = facts.get("objects") or []
    keywords = summary.get("keywords") or []
    char = characters[0] if characters else ""
    obj = objects[0] if objects else ""
    keyword = keywords[0] if keywords else ""
    limit = MAX_LEN[band]
    avoid = avoid or []

    if language == "en":
        subject = _en_subject(char)
        if qtype == "guess":
            text = f"Where do you think {subject} will go next?"
        elif qtype == "retell":
            text = "Who was on the page just now?"
        elif qtype == "empathy":
            text = f"How does {subject} feel now?"
        elif char:
            text = f"What is {subject} doing?"
        elif obj:
            text = f"What is this {obj}?"
        else:
            text = "What can you see on this page?"
        return {
            "question": text,
            "type": qtype,
            "expected": obj or char,
            "hint": "用英文提问，允许孩子中英混答",
            "choices": [item for item in (char, obj) if item][:3],
            "language": "en",
        }

    if band == "under3":
        choices = [char] if char else []
        distractor = _distractor(characters + objects)
        if distractor:
            choices.append(distractor)
        text = f"这是{char}吗？" if char else "看看这是什么？"
        return {
            "question": truncate(text, 8),
            "type": "observe",
            "expected": char,
            "hint": "配两个大图，孩子点一下就行",
            "choices": choices,
            "language": "zh",
        }

    if qtype == "guess":
        text = f"你觉得{char}接下来会去哪里？" if char else "你觉得接下来会发生什么？"
    elif qtype == "empathy":
        text = f"你觉得{char}心里什么感觉呀？" if char else "你觉得画面里的人开心吗？"
    elif qtype == "retell":
        text = f"刚才这一页，谁拿着{obj}？" if obj else "刚才这一页里有谁呀？"
    elif qtype == "life":
        text = f"你也玩过{obj or keyword}吗？" if (obj or keyword) else "你也见过这样的画面吗？"
    elif obj:
        text = f"{char}手里拿着什么呀？" if char else f"画面里有什么呀？"
    elif char:
        text = f"{char}在做什么呀？"
    else:
        text = "你在画面上看到了什么呀？"

    for banned in avoid:
        if banned and banned in text:
            text = f"{char}在做什么呀？" if char else "你在画面上看到了什么呀？"

    return {
        "question": truncate(text, max(limit, 8)),
        "type": qtype,
        "expected": obj or char or keyword,
        "hint": "等孩子说完再回应，不急着补充",
        "choices": [item for item in (char, obj) if item][:3],
        "language": "zh",
    }


def _en_subject(char: str) -> str:
    """英文提问补冠词：“cat” → “the cat”，专有名词不动。"""
    if not char:
        return "the little one"
    if char[0].isupper() or char.startswith(("the ", "a ", "an ")):
        return char
    return f"the {char}"


def _distractor(present: list[str]) -> str:
    for noun in lexicon.PICTURE_NOUNS:
        if noun not in present:
            return noun
    return ""


def understand(facts: dict, question_text: str, answer: str, age_months: int, confidence: float) -> dict:
    """A7：只判"听没听懂"，不打分、不贴标签。"""
    answer = (answer or "").strip()
    expected = " ".join(
        [str(item) for item in (facts.get("characters") or []) + (facts.get("objects") or [])]
    )
    if not answer:
        return {
            "understood": "no",
            "action": "icon_choice",
            "reply": "没关系，我们一起看看这里……",
            "confidence": 0.0,
        }
    if confidence and confidence < 0.6:
        return {
            "understood": "partial",
            "action": "confirm",
            "reply": f"你是不是说「{truncate(answer, 8)}」？",
            "confidence": confidence,
        }
    if any(word in answer for word in ("不知道", "不会", "不懂", "忘了")):
        hint = _answer_hint(facts)
        return {
            "understood": "partial",
            "action": "give_answer",
            "reply": f"没关系，{hint}",
            "confidence": confidence,
        }
    if "为什么" in answer:
        return {
            "understood": "partial",
            "action": "answer_why",
            "reply": "因为它们想一起玩呀，你看它们笑得多开心。",
            "confidence": confidence,
        }
    hit = [noun for noun in lexicon.find_nouns(answer) if noun in expected]
    if hit:
        return {
            "understood": "yes",
            "action": "encourage",
            "reply": f"哇，你看到{hit[0]}了，真仔细！",
            "confidence": confidence,
        }
    if len(normalize(answer)) >= 2:
        return {
            "understood": "partial",
            "action": "extend",
            "reply": f"嗯，你说「{truncate(answer, 8)}」了。我们一起看看画面里还有什么。",
            "confidence": confidence,
        }
    return {
        "understood": "partial",
        "action": "confirm",
        "reply": "嗯，我们一起再看看这里。",
        "confidence": confidence,
    }


def _answer_hint(facts: dict) -> str:
    characters = facts.get("characters") or []
    objects = facts.get("objects") or []
    if characters and objects:
        return f"{characters[0]}手里拿着{objects[0]}呀。"
    if characters:
        relation = (facts.get("relations") or [""])[0]
        return f"你看，{relation or characters[0] + '在画面里'}。"
    if objects:
        return f"画面里有{objects[0]}呀。"
    return "我们一起看看画面吧。"


def advice(child: dict, turns: list[dict]) -> list[dict]:
    """A10 规则版：每条建议都绑定 evidence（问答记录 id），无依据不生成。"""
    if not turns:
        return []
    total = len(turns)
    answered = [t for t in turns if (t.get("answer_text") or "").strip()]
    unanswered = [t for t in turns if not (t.get("answer_text") or "").strip()]
    yes = [t for t in turns if t.get("understood") == "yes"]
    partial = [t for t in turns if t.get("understood") == "partial"]
    closed = [t for t in turns if lexicon.is_closed_question(t.get("question") or "")]
    band = age_band(int(child.get("age_months") or 48))
    items: list[dict] = []

    if unanswered:
        items.append(
            {
                "text": f"本次有 {len(unanswered)} 次没等到回答，建议下次先示范一句，或换成图标题。",
                "category": "waiting",
                "evidence": [t["id"] for t in unanswered][:3],
            }
        )
    if yes:
        keyword = _first_keyword(yes[0], turns)
        items.append(
            {
                "text": f"孩子能说出「{keyword}」，可延伸同主题绘本再做一次共读。",
                "category": "interest",
                "evidence": [t["id"] for t in yes][:3],
            }
        )
    if partial:
        items.append(
            {
                "text": f"有 {len(partial)} 次只说了短句，建议多等 3 秒再把问题说一遍。",
                "category": "waiting",
                "evidence": [t["id"] for t in partial][:3],
            }
        )
    if len(closed) >= 2:
        items.append(
            {
                "text": "本次封闭题偏多，下次多用“你看到了什么”这类开放题。",
                "category": "question_mix",
                "evidence": [t["id"] for t in closed][:3],
            }
        )
    if band == "under3":
        items.append(
            {
                "text": "这个年龄更适合两个大图的二选一，提问句控制在 8 个字内。",
                "category": "age_fit",
                "evidence": [t["id"] for t in turns[:2]],
            }
        )
    if len(items) < 3:
        items.append(
            {
                "text": f"本次共读完成 {total} 页，节奏平稳；下次可以换一本同主题绘本。",
                "category": "pace",
                "evidence": [t["id"] for t in turns[:3]],
            }
        )
    if len(answered) == total and len(items) < 3:
        items.append(
            {
                "text": "孩子每页都有回应，可以把等待时间再缩短一点，保持节奏。",
                "category": "pace",
                "evidence": [t["id"] for t in answered[:3]],
            }
        )
    return [item for item in items if item["evidence"]][:5]


def _first_keyword(turn: dict, turns: list[dict]) -> str:
    checker = turn.get("checker") or {}
    for key in ("expected", "keyword"):
        value = checker.get(key)
        if value:
            return str(value)
    answer = turn.get("answer_text") or ""
    nouns = lexicon.find_nouns(answer)
    if nouns:
        return nouns[0]
    return truncate(answer, 6) or "这个画面"