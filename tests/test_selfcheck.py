"""A11 内容闸门的单元测试：拦截必须是确定的、可解释的。"""

from __future__ import annotations

from app.services import selfcheck

CHECKER = selfcheck.ContentChecker()

FACTS = {
    "raw_text": "小兔子在草地上追风筝。",
    "characters": ["小兔子"],
    "objects": ["风筝", "草地"],
    "relations": ["小兔子追风筝"],
    "scene": "草地",
    "keywords": ["小兔子", "风筝"],
}


def check(question: str, *, age_months: int = 54, history=None, language: str = "zh", qtype: str = "observe"):
    return CHECKER.check_question(
        {"question": question, "type": qtype},
        FACTS,
        age_months=age_months,
        language=language,
        history=history or [],
    )


def ids(report) -> set[str]:
    return {check.id for check in report.failures}


def test_good_question_passes():
    report = check("小兔子在草地上追什么呀？")
    assert report.verdict == "pass", report.to_dict()
    assert report.score == 100.0


def test_off_page_entity_is_blocked():
    # 画面里没有大象，问到它就是"编造"
    report = check("大象躲到哪里去了呀？")
    assert "question_grounding" in ids(report)


def test_abstract_word_is_blocked():
    report = check("小兔子现在的心情是怎样的呀？", qtype="empathy")
    assert "question_abstract" in ids(report)


def test_two_questions_in_one_is_blocked():
    report = check("小兔子在做什么呀？风筝飞得高不高呀？")
    assert "single_question" in ids(report)


def test_no_question_mark_is_only_a_warning():
    report = check("小兔子在草地上追风筝")
    status = {item.id: item.status for item in report.checks}
    assert status["single_question"] == "warn"
    assert report.verdict == "pass"


def test_english_question_with_halfwidth_mark_passes():
    facts = {"raw_text": "The cat sat on the mat.", "characters": ["cat"], "objects": ["mat"]}
    report = CHECKER.check_question(
        {"question": "What is the cat sitting on?", "type": "observe"},
        facts,
        age_months=54,
        language="en",
        history=[],
    )
    status = {item.id: item.status for item in report.checks}
    assert status["single_question"] == "ok"


def test_long_question_is_blocked_for_age():
    long_question = "小兔子在草地上追着风筝跑来跑去的时候，旁边的小草被风吹得晃来晃去吗？"
    report = check(long_question, age_months=30)  # 2 岁半，上限 8 字
    assert "question_length" in ids(report)


def test_repeat_question_is_blocked():
    history = [{"question": "小兔子在草地上追什么呀？", "answer": "风筝"}]
    report = check("小兔子在草地上追什么呀？", history=history)
    assert "question_repeat" in ids(report)


def test_closed_question_quota():
    history = [{"question": "这是小兔子吗？"}, {"question": "这是风筝吗？"}]
    report = check("这是草地吗？", history=history)
    status = {item.id: item.status for item in report.checks}
    assert status["question_closed_quota"] == "warn"


def test_summary_raw_text_must_come_from_ocr():
    facts = {"raw_text": "小兔子拿着风筝。", "characters": ["小兔子"], "objects": ["风筝"]}
    summary = {
        "raw_text": "小兔子拿着气球。",
        "faithful_description": "画面里有小兔子。小兔子拿着气球。",
        "read_aloud_script": "看呀，小兔子拿着气球。",
    }
    report = CHECKER.check_summary(facts, summary, age_months=54, language="zh")
    assert "raw_text_locked" in ids(report)


def test_summary_cannot_invent_entities():
    facts = {"raw_text": "小兔子拿着风筝。", "characters": ["小兔子"], "objects": ["风筝"]}
    summary = {
        "raw_text": facts["raw_text"],
        "faithful_description": "画面里有小兔子。小兔子拿着风筝。旁边有一只大象。",
        "read_aloud_script": "看呀，小兔子拿着风筝。",
    }
    report = CHECKER.check_summary(facts, summary, age_months=54, language="zh")
    assert "desc_grounding" in ids(report)


def test_holding_phrase_is_not_off_page():
    # “手里”是老师习惯说法，不该把“手”判成画外实体
    report = check("小兔子手里拿着什么呀？")
    assert report.verdict == "pass", report.to_dict()


def test_english_sentences_use_word_limit():
    facts = {"raw_text": "The cat sat on the mat.", "characters": ["cat"], "objects": ["mat"]}
    summary = {
        "raw_text": facts["raw_text"],
        "faithful_description": "You can see a cat. The cat sits on a mat.",
        "read_aloud_script": "You can see a cat.",
    }
    report = CHECKER.check_summary(facts, summary, age_months=54, language="en")
    status = {item.id: item.status for item in report.checks}
    assert status["desc_sentence_length"] == "ok"


def test_english_question_length_uses_words():
    facts = {"raw_text": "The cat sat on the mat.", "characters": ["cat"], "objects": ["mat"]}
    report = CHECKER.check_question(
        {"question": "What is the cat sitting on?", "type": "observe"},
        facts,
        age_months=54,
        language="en",
        history=[],
    )
    status = {item.id: item.status for item in report.checks}
    assert status["question_length"] == "ok"
    assert report.verdict == "pass"


def test_safe_question_only_uses_facts():
    safe = CHECKER.safe_question(FACTS, 54, "zh")
    assert safe["mode"] == "safe_template"
    assert "小兔子" in safe["question"] or "风筝" in safe["question"]
    assert check(safe["question"]).verdict == "pass"


def test_advice_requires_evidence_and_no_ranking():
    turns = [{"id": 1, "question": "小兔子拿着什么呀？", "answer_text": "风筝"}]
    child = {"name": "小雨", "age_months": 54, "guardian_consent": 1}
    good = CHECKER.check_advice(
        [{"text": "孩子能说出「风筝」，可以接着读同主题绘本。", "evidence": [1]}], turns, child
    )
    assert "advice_evidence" not in ids(good)

    bad = CHECKER.check_advice(
        [
            {"text": "孩子的语言能力比同班强。", "evidence": [1]},
            {"text": "多看绘本有好处。", "evidence": []},
        ],
        turns,
        child,
    )
    assert "advice_no_ranking" in ids(bad)
    assert "advice_evidence" in ids(bad)

def test_system_check_returns_plain_language():
    result = selfcheck.run_system_check()
    assert result["checks"], "运行自检至少要给出检查项"
    for item in result["checks"]:
        assert item["name"], "检查项必须有中文名"
        assert item["status"] in {"ok", "warn", "fail"}