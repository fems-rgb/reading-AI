"""A3/A4/A7/A10 的"大脑"：优先本地 LLM，失败或未接入时退到确定性规则。

关键约束写在这里而不是提示词里：
- raw_text 永远取自 OCR，模型无权改写（代码强制覆盖）；
- 问题必须落在允许的题型集合内，字段缺失时补默认值；
- 任何 LLM 异常都不阻断共读，只降级并留痕（mode=rule/degraded）。
"""

from __future__ import annotations

import json
from typing import Any

from .. import db, lexicon
from ..utils import truncate
from . import nlg
from .llm import LLMError, LocalLLM
from .metrics import Tracer

QUESTION_TYPES = set(nlg.TYPE_LABELS)
UNDERSTOOD = {"yes", "partial", "no"}
ACTIONS = {"encourage", "confirm", "give_answer", "answer_why", "icon_choice"}


def _has_chinese(text: str) -> bool:
    """本机小模型偶尔会切成英文；面向孩子的话必须是中文，否则回退到规则文案。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _ungrounded_entities(text: str, facts: dict) -> list[str]:
    """问题里出现画面事实之外的名词就算没接地。

    本机小模型会把提示词里的 JSON 示例原样照抄（实测连问三轮都是
    “小兔子手里拿着什么？”）；闸门能拦住，但孩子已经听到了，所以在这里
    先拦一道：不接地就直接换规则题，规则题只从 facts 里取词。
    """
    from .selfcheck import fact_nouns, probe_text

    return sorted(set(lexicon.find_nouns(probe_text(text))) - fact_nouns(facts))


class Brain:
    def __init__(self, tracer: Tracer | None = None) -> None:
        self.llm = LocalLLM()
        self.tracer = tracer

    def _prompt(self, name: str, fallback: str) -> tuple[str, int]:
        row = db.get_prompt(name)
        if row:
            return str(row["body"]), int(row["version"])
        return fallback, 0

    async def fuse(self, facts: dict, age_months: int, language: str, avoid: list[str] | None = None) -> dict:
        rule_based = nlg.fuse(facts, age_months, language)
        if not self.llm.enabled:
            return {**rule_based, "mode": "rule"}
        system, version = self._prompt("A3_fuse", "只使用给定事实，输出 JSON 三层摘要。")
        user = json.dumps(
            {
                "ocr_text": facts.get("raw_text", ""),
                "characters": facts.get("characters", []),
                "objects": facts.get("objects", []),
                "relations": facts.get("relations", []),
                "scene": facts.get("scene", ""),
                "age_months": age_months,
                "language": language,
                "avoid_entities": avoid or [],
            },
            ensure_ascii=False,
        )
        try:
            parsed, meta = await _run(self.tracer, "A3_fuse", self.llm.chat_json, system, user)
            if not isinstance(parsed, dict):
                raise LLMError("A3 输出不是对象")
            return {
                "raw_text": facts.get("raw_text", ""),  # 强制以 OCR 为准
                "faithful_description": str(parsed.get("faithful_description") or rule_based["faithful_description"]),
                "read_aloud_script": str(parsed.get("read_aloud_script") or rule_based["read_aloud_script"]),
                "speculative_flags": list(parsed.get("speculative_flags") or []),
                "keywords": list(parsed.get("keywords") or rule_based["keywords"]),
                "language": language,
                "band": rule_based["band"],
                "mode": "llm",
                "prompt_version": version,
                "tokens": meta,
            }
        except Exception:
            return {**rule_based, "mode": "rule", "degraded_reason": "llm_failed"}

    async def question(
        self,
        facts: dict,
        summary: dict,
        age_months: int,
        language: str,
        qtype: str,
        history: list[dict],
        avoid: list[str] | None = None,
    ) -> dict:
        qtype = qtype if qtype in QUESTION_TYPES else "observe"
        rule_based = nlg.question(facts, summary, age_months, language, qtype, history, avoid)
        if not self.llm.enabled:
            return {**rule_based, "mode": "rule"}
        system, version = self._prompt("A4_question", "提一个适龄、只含画面事实的问题，输出 JSON。")
        max_len = nlg.MAX_LEN.get(summary.get("band") or "4_6", 20)
        system = system.replace("{max_len}", str(max_len))
        user = json.dumps(
            {
                "page": {
                    "characters": facts.get("characters", []),
                    "objects": facts.get("objects", []),
                    "scene": facts.get("scene", ""),
                    "relations": facts.get("relations", []),
                    "keywords": summary.get("keywords", []),
                    "faithful_description": summary.get("faithful_description", ""),
                },
                "age_months": age_months,
                "language": language,
                "question_type": qtype,
                "max_chars": max_len,
                "recent_questions": [item.get("question") for item in history[-3:]],
                "avoid_entities": avoid or [],
            },
            ensure_ascii=False,
        )
        try:
            parsed, meta = await _run(self.tracer, "A4_question", self.llm.chat_json, system, user)
            if not isinstance(parsed, dict) or not str(parsed.get("question") or "").strip():
                raise LLMError("A4 输出缺少 question")
            if not _has_chinese(str(parsed["question"])):
                raise LLMError("A4 输出不是中文")
            question_text = truncate(str(parsed["question"]).strip(), max(max_len, 8))
            ungrounded = _ungrounded_entities(question_text, facts)
            if ungrounded:
                return {
                    **rule_based,
                    "mode": "rule",
                    "degraded_reason": "ungrounded_question",
                    "ungrounded": ungrounded,
                }
            return {
                "question": question_text,
                "type": str(parsed.get("type") or qtype),
                "expected": str(parsed.get("expected") or rule_based["expected"]),
                "hint": str(parsed.get("hint") or rule_based["hint"]),
                "choices": rule_based["choices"],
                "language": language,
                "mode": "llm",
                "prompt_version": version,
                "tokens": meta,
            }
        except Exception:
            return {**rule_based, "mode": "rule", "degraded_reason": "llm_failed"}

    async def understand(
        self,
        facts: dict,
        question_text: str,
        answer: str,
        age_months: int,
        confidence: float,
    ) -> dict:
        rule_based = nlg.understand(facts, question_text, answer, age_months, confidence)
        if not self.llm.enabled:
            return {**rule_based, "mode": "rule"}
        system, version = self._prompt("A7_understand", "判断理解程度，不打分，输出 JSON。")
        user = json.dumps(
            {
                "question": question_text,
                "answer": answer,
                "asr_confidence": confidence,
                "age_months": age_months,
                "visible_facts": {
                    "characters": facts.get("characters", []),
                    "objects": facts.get("objects", []),
                    "scene": facts.get("scene", ""),
                    "relations": facts.get("relations", []),
                },
            },
            ensure_ascii=False,
        )
        try:
            parsed, meta = await _run(self.tracer, "A7_understand", self.llm.chat_json, system, user)
            if not isinstance(parsed, dict):
                raise LLMError("A7 输出不是对象")
            understood = str(parsed.get("understood") or "").lower()
            action = str(parsed.get("action") or "").lower()
            if understood not in UNDERSTOOD or action not in ACTIONS:
                raise LLMError("A7 字段取值越界")
            reply = str(parsed.get("reply") or "")
            if not _has_chinese(reply):
                raise LLMError("A7 回复不是中文")
            return {
                "understood": understood,
                "action": action,
                # 话术用规则模板：小模型会把问句和答案说反，孩子听到的话必须可控
                "reply": nlg.reply_for(action, facts, answer, confidence),
                "confidence": confidence,
                "mode": "llm",
                "prompt_version": version,
                "tokens": meta,
            }
        except Exception:
            return {**rule_based, "mode": "rule", "degraded_reason": "llm_failed"}

    async def advise(self, child: dict, turns: list[dict]) -> list[dict]:
        rule_based = nlg.advice(child, turns)
        if not self.llm.enabled or not turns:
            return rule_based
        system, _version = self._prompt("A10_advice", "给老师 3-5 条建议，每条带 evidence，不排名。")
        condensed = [
            {
                "id": turn["id"],
                "question": turn.get("question", ""),
                "answer": turn.get("answer_text", ""),
                "understood": turn.get("understood", ""),
            }
            for turn in turns
        ]
        user = json.dumps(
            {"child_age_months": child.get("age_months"), "turns": condensed}, ensure_ascii=False
        )
        try:
            parsed, _meta = await _run(self.tracer, "A10_advice", self.llm.chat_json, system, user, json_mode=False)
            if not isinstance(parsed, list):
                raise LLMError("A10 输出不是数组")
            valid_ids = {turn["id"] for turn in turns}
            cleaned = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                evidence = [int(i) for i in (item.get("evidence") or []) if str(i).isdigit()]
                evidence = [i for i in evidence if i in valid_ids]
                if not item.get("text") or not evidence:
                    continue
                if not _has_chinese(str(item["text"])):
                    continue
                cleaned.append(
                    {
                        "text": truncate(str(item["text"]), 60),
                        "category": str(item.get("category") or "general"),
                        "evidence": evidence[:3],
                    }
                )
            return cleaned[:5] or rule_based
        except Exception:
            return rule_based


async def _run(tracer: Tracer | None, stage: str, fn, system: str, user: str, **kwargs):
    import asyncio

    if tracer is None:
        return await asyncio.to_thread(fn, system, user, **kwargs)
    with tracer.timed(stage) as slot:
        parsed, meta = await asyncio.to_thread(fn, system, user, **kwargs)
        slot["tokens_in"] = meta.get("tokens_in", 0)
        slot["tokens_out"] = meta.get("tokens_out", 0)
        slot["model"] = meta.get("model", "")
        slot["provider"] = "local_llm"
        return parsed, meta