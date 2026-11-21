"""A11 AI 自检 —— 原方案缺失的那一环。

三层自检，缺一不可：

1. **内容自检（闸门）**：每一条要念给孩子听的文本（三层摘要、提问、教学建议、
   家长周报）在出口前过一遍规则检查。发现问题不是"记个日志"，而是当场
   重写或降级：大模型改一次 → 再检 → 仍不合格就换成"保证接地"的安全模板。
   孩子听到的每一句话，都是过了闸门的。
2. **运行自检**：开场前检查数据库、存储、园内模型服务、离线出站、绘本库、
   监护授权。失败给人话提示 + 下一步按钮，不把 `VLM timeout` 甩给老师。
3. **会话后自检**：复盘记录是否完整、每条教学建议是否有依据、临时数据是否
   已释放。规范 §8 的"无依据建议数 = 0"由这里兜底。

为什么不交给 LLM 自己判断：自检必须是确定性的、可复现的、能解释的。
LLM 只在"改一版"时参与，裁判永远是规则。
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .. import db, guard, lexicon
from ..config import get_settings
from ..utils import age_band, now, similarity, split_sentences, truncate
from . import nlg

MAX_LEN = {"under3": 8, "3_4": 12, "4_6": 20}
SENTENCE_LIMIT = 15
REPEAT_THRESHOLD = 0.75


@dataclass
class Check:
    id: str
    name: str
    status: str  # ok / warn / fail
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class Report:
    scope: str
    checks: list[Check] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def verdict(self) -> str:
        return "block" if self.failures else "pass"

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        penalty = len(self.failures) * 30 + len(self.warnings) * 8
        return round(max(0.0, 100.0 - penalty), 1)

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "verdict": self.verdict,
            "score": self.score,
            "checks": [c.to_dict() for c in self.checks],
            "actions": self.actions,
        }

    def summary_line(self) -> str:
        if not self.checks:
            return "未执行检查"
        if self.failures:
            return "；".join(f"{c.detail or c.name}" for c in self.failures[:2])
        if self.warnings:
            return "；".join(f"{c.detail or c.name}" for c in self.warnings[:2])
        return "全部检查通过"


def fact_nouns(facts: dict) -> set[str]:
    parts = [
        str(facts.get("raw_text") or ""),
        str(facts.get("scene") or ""),
        " ".join(facts.get("characters") or []),
        " ".join(facts.get("objects") or []),
        " ".join(facts.get("relations") or []),
        " ".join(facts.get("keywords") or []),
    ]
    return set(lexicon.find_nouns(" ".join(parts)))


# 老师习惯说“手里拿着什么”，“手”属于提问习惯用语，不当成画外实体；
# “画面/图片”是元话语，不是画面里的东西。
_PROBE_PHRASES = ("手里", "手上", "手里边", "身边", "旁边", "怀里", "画面", "图片")


def probe_text(text: str) -> str:
    """抽实体前先去掉习惯用语，避免把“手里”当成画面外的“手”。"""
    cleaned = text or ""
    for phrase in _PROBE_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    return cleaned


class ContentChecker:
    """内容闸门本体：纯函数，易测试、可复现。"""

    # ---------- A3 三层摘要 ----------

    def check_summary(self, facts: dict, summary: dict, *, age_months: int, language: str) -> Report:
        report = Report("content")
        allowed = fact_nouns(facts)
        desc = str(summary.get("faithful_description") or "")
        script = str(summary.get("read_aloud_script") or "")
        raw = str(summary.get("raw_text") or "")
        flags = summary.get("speculative_flags") or []

        # 1. 原文层必须等于 OCR，模型无权改写
        ocr_text = str(facts.get("raw_text") or "").strip()
        if ocr_text and raw.strip() != ocr_text:
            report.checks.append(
                Check("raw_text_locked", "原文层未被改写", "fail", "原文层与 OCR 不一致", "恢复 OCR 原文")
            )
        else:
            report.checks.append(Check("raw_text_locked", "原文层未被改写", "ok"))

        # 2. 忠实描述不得出现画面里没有的具体名词
        extra = sorted(set(lexicon.find_nouns(probe_text(desc))) - allowed)
        if extra:
            report.checks.append(
                Check(
                    "desc_grounding",
                    "忠实描述只含画面内容",
                    "fail",
                    f"描述里出现了画面没有的「{'、'.join(extra[:3])}」",
                    f"删除或替换：{'、'.join(extra[:3])}",
                )
            )
            report.actions.append({"type": "rewrite_summary", "avoid": extra[:5]})
        else:
            report.checks.append(Check("desc_grounding", "忠实描述只含画面内容", "ok"))

        # 3. 推测必须进 speculative_flags
        speculative = lexicon.find_speculative(desc)
        if speculative and not flags:
            report.checks.append(
                Check(
                    "desc_speculation",
                    "推测内容已标注",
                    "fail",
                    f"描述里有推测词「{'、'.join(speculative[:2])}」但没有标注",
                    "改为可见事实，或写入 speculative_flags",
                )
            )
            report.actions.append({"type": "rewrite_summary", "avoid": speculative[:3]})
        else:
            report.checks.append(Check("desc_speculation", "推测内容已标注", "ok"))

        # 4. 句子长度适龄（中文按字，英文按词）
        if language == "en":
            long_sentences = [s for s in split_sentences(desc) if len(s.split()) > SENTENCE_LIMIT - 3]
        else:
            long_sentences = [s for s in split_sentences(desc) if len(s) > SENTENCE_LIMIT]
        if long_sentences:
            report.checks.append(
                Check(
                    "desc_sentence_length",
                    "句子长度适龄",
                    "warn",
                    f"有 {len(long_sentences)} 句过长",
                    "拆成更短的句子",
                )
            )
        else:
            report.checks.append(Check("desc_sentence_length", "句子长度适龄", "ok"))

        # 5. 讲稿不得引入前两层之外的新实体
        script_extra = sorted(
            set(lexicon.find_nouns(probe_text(script))) - allowed - set(lexicon.find_nouns(probe_text(desc)))
        )
        if script_extra:
            report.checks.append(
                Check(
                    "script_new_entity",
                    "讲稿未新增实体",
                    "fail",
                    f"讲稿里新增了「{'、'.join(script_extra[:3])}」",
                    f"删除：{'、'.join(script_extra[:3])}",
                )
            )
            report.actions.append({"type": "rewrite_script", "avoid": script_extra[:5]})
        else:
            report.checks.append(Check("script_new_entity", "讲稿未新增实体", "ok"))

        # 6. 不得自行推进剧情
        plot_words = [w for w in ("后来", "终于", "结果", "突然", "最后", "从此") if w in script]
        if plot_words:
            report.checks.append(
                Check(
                    "script_plot_invention",
                    "讲稿不推进剧情",
                    "warn",
                    f"讲稿出现情节推进词「{'、'.join(plot_words)}」",
                    "删掉时间推进词，只描述当前页",
                )
            )
            report.actions.append({"type": "rewrite_script", "avoid": plot_words})
        else:
            report.checks.append(Check("script_plot_invention", "讲稿不推进剧情", "ok"))

        # 7. 语言一致
        if language == "en":
            ok_language = lexicon.latin_ratio(desc + script) >= 0.5
        else:
            ok_language = lexicon.cjk_ratio(desc) >= 0.3
        report.checks.append(
            Check(
                "summary_language",
                "输出语言与绘本一致",
                "ok" if ok_language else "warn",
                "" if ok_language else "文本语言与绘本语言不一致",
                "按 book.language 重新生成",
            )
        )
        return report

    # ---------- A4 提问 ----------

    def check_question(
        self,
        question: dict,
        facts: dict,
        *,
        age_months: int,
        language: str,
        history: list[dict],
        allowed_extra: set[str] | None = None,
    ) -> Report:
        report = Report("content")
        text = str(question.get("question") or "").strip()
        qtype = str(question.get("type") or "observe")
        band = age_band(age_months)
        limit = MAX_LEN[band]
        allowed = fact_nouns(facts) | set(allowed_extra or ())

        if not text:
            report.checks.append(Check("question_present", "问题非空", "fail", "没有生成出问题", "重新生成"))
            return report
        report.checks.append(Check("question_present", "问题非空", "ok"))

        # 1. 一次只问一个
        marks = text.count("？") + text.count("?")
        if marks > 1:
            report.checks.append(
                Check("single_question", "一次只问一个问题", "fail", f"检测到 {marks} 个问号", "只保留一个问题")
            )
            report.actions.append({"type": "regenerate_question", "single_question": True})
        elif marks == 0:
            report.checks.append(
                Check("single_question", "一次只问一个问题", "warn", "问题没有问号，可能不是提问", "改成明确的问句")
            )
        else:
            report.checks.append(Check("single_question", "一次只问一个问题", "ok"))

        # 2. 长度适龄（中文按字数，英文按词数，否则英文题会被误判过长）
        if language == "en":
            words = len(text.split())
            if words > 14:
                report.checks.append(
                    Check("question_length", "句子长度适龄", "fail", f"问题 {words} 个词，太长", "控制在 10 词内")
                )
                report.actions.append({"type": "regenerate_question", "max_words": 10})
            elif words > 10:
                report.checks.append(
                    Check("question_length", "句子长度适龄", "warn", f"问题 {words} 个词，略长", "再短一点")
                )
            else:
                report.checks.append(Check("question_length", "句子长度适龄", "ok"))
        elif len(text) > limit + 6:
            report.checks.append(
                Check("question_length", "句子长度适龄", "fail", f"问题 {len(text)} 字，超过 {limit} 字上限", f"压缩到 {limit} 字内")
            )
            report.actions.append({"type": "regenerate_question", "max_chars": limit})
        elif len(text) > limit:
            report.checks.append(
                Check("question_length", "句子长度适龄", "warn", f"问题 {len(text)} 字，略超 {limit} 字", "再短一点")
            )
        else:
            report.checks.append(Check("question_length", "句子长度适龄", "ok"))

        # 3. 抽象词
        abstract = lexicon.find_abstract(text)
        if abstract:
            report.checks.append(
                Check(
                    "question_abstract",
                    "不含抽象词",
                    "fail",
                    f"出现幼儿听不懂的抽象词「{'、'.join(abstract)}」",
                    "换成看得见的具体词",
                )
            )
            report.actions.append({"type": "regenerate_question", "avoid": abstract})
        else:
            report.checks.append(Check("question_abstract", "不含抽象词", "ok"))

        # 4. 接地：不得问到画面里没有的东西
        extra = sorted(set(lexicon.find_nouns(probe_text(text))) - allowed)
        if extra:
            report.checks.append(
                Check(
                    "question_grounding",
                    "只问画面里的内容",
                    "fail",
                    f"问题提到画面没有的「{'、'.join(extra[:3])}」",
                    f"不要提到：{'、'.join(extra[:3])}",
                )
            )
            report.actions.append({"type": "regenerate_question", "avoid": extra[:5]})
        else:
            report.checks.append(Check("question_grounding", "只问画面里的内容", "ok"))

        # 5. 不重复最近三题
        worst = 0.0
        worst_text = ""
        for item in history[-3:]:
            score = similarity(text, str(item.get("question") or ""))
            if score > worst:
                worst, worst_text = score, str(item.get("question") or "")
        if worst >= REPEAT_THRESHOLD:
            report.checks.append(
                Check(
                    "question_repeat",
                    "不重复最近的问题",
                    "fail",
                    f"与「{truncate(worst_text, 12)}」相似度 {worst:.2f}",
                    "换一个角度提问",
                )
            )
            report.actions.append({"type": "regenerate_question", "avoid_repeat": True, "max_chars": limit})
        else:
            report.checks.append(Check("question_repeat", "不重复最近的问题", "ok"))

        # 6. 封闭题配额：每 3 题最多 1 道
        if lexicon.is_closed_question(text):
            recent_closed = sum(
                1 for item in history[-2:] if lexicon.is_closed_question(str(item.get("question") or ""))
            )
            if recent_closed >= 1:
                report.checks.append(
                    Check(
                        "question_closed_quota",
                        "封闭题不超标",
                        "warn",
                        "最近两题里已经有一道封闭题",
                        "改成开放题，如“你看到了什么”",
                    )
                )
            else:
                report.checks.append(Check("question_closed_quota", "封闭题不超标", "ok"))
        else:
            report.checks.append(Check("question_closed_quota", "封闭题不超标", "ok"))

        # 7. 语言一致
        if language == "en":
            ok_language = lexicon.latin_ratio(text) >= 0.6
        else:
            ok_language = lexicon.cjk_ratio(text) >= 0.4
        report.checks.append(
            Check(
                "question_language",
                "提问语言与绘本一致",
                "ok" if ok_language else "fail",
                "" if ok_language else "提问语言与绘本语言不一致",
                "用绘本语言重新提问",
            )
        )
        if not ok_language:
            report.actions.append({"type": "regenerate_question", "language": language})

        # 8. 题型合法
        report.checks.append(
            Check(
                "question_type",
                "题型在允许范围内",
                "ok" if qtype in nlg.TYPE_LABELS else "warn",
                "" if qtype in nlg.TYPE_LABELS else f"未知题型 {qtype}",
                "归一到观察型",
            )
        )
        return report

    # ---------- A10 建议 / 周报 ----------

    def check_advice(self, items: list[dict], turns: list[dict], child: dict) -> Report:
        report = Report("content")
        valid_ids = {turn["id"] for turn in turns}
        if not items:
            report.checks.append(Check("advice_present", "生成了教学建议", "fail", "没有生成任何建议", "重新生成"))
            return report
        report.checks.append(Check("advice_present", "生成了教学建议", "ok"))

        missing = [item for item in items if not item.get("evidence")]
        invalid = [
            item for item in items
            if item.get("evidence") and not set(item["evidence"]) <= valid_ids
        ]
        if missing or invalid:
            report.checks.append(
                Check(
                    "advice_evidence",
                    "每条建议都可追溯",
                    "fail",
                    f"{len(missing) + len(invalid)} 条建议没有有效依据",
                    "删除无依据建议",
                )
            )
            report.actions.append({"type": "drop_unevidenced_advice"})
        else:
            report.checks.append(Check("advice_evidence", "每条建议都可追溯", "ok"))

        ranking = [(item, lexicon.find_ranking(item.get("text") or "")) for item in items]
        ranking = [(item, words) for item, words in ranking if words]
        if ranking:
            report.checks.append(
                Check(
                    "advice_no_ranking",
                    "不含排名/能力标签",
                    "fail",
                    f"出现「{'、'.join(ranking[0][1])}」等词",
                    "改写为描述行为，不评价孩子",
                )
            )
        else:
            report.checks.append(Check("advice_no_ranking", "不含排名/能力标签", "ok"))

        too_long = [item for item in items if len(item.get("text") or "") > 60]
        report.checks.append(
            Check(
                "advice_length",
                "建议长度适合老师扫读",
                "warn" if too_long else "ok",
                f"{len(too_long)} 条超过 60 字" if too_long else "",
                "压缩到 60 字内",
            )
        )
        if not 3 <= len(items) <= 5:
            report.checks.append(
                Check("advice_count", "建议数量 3-5 条", "warn", f"当前 {len(items)} 条", "补齐到 3 条")
            )
        else:
            report.checks.append(Check("advice_count", "建议数量 3-5 条", "ok"))
        return report

    def check_report(self, payload: dict, child: dict) -> Report:
        report = Report("content")
        text = " ".join(str(payload.get(key) or "") for key in ("headline", "summary", "teacher_note"))
        text += " ".join(str(item) for item in payload.get("highlights") or [])
        ranking = lexicon.find_ranking(text)
        report.checks.append(
            Check(
                "report_no_ranking",
                "周报不排名不贴标签",
                "fail" if ranking else "ok",
                f"出现「{'、'.join(ranking)}」" if ranking else "",
                "删除比较性表述",
            )
        )
        forbidden = [key for key in payload if key in {"audio", "image", "video", "raw_audio", "base64"}]
        report.checks.append(
            Check(
                "report_no_media",
                "周报不含原始音视频",
                "fail" if forbidden else "ok",
                f"包含 {forbidden}" if forbidden else "",
                "移除媒体字段",
            )
        )
        consented = bool(child.get("guardian_consent"))
        report.checks.append(
            Check(
                "report_consent",
                "已获监护人授权",
                "ok" if consented else "fail",
                "" if consented else "该幼儿尚未获得监护人书面授权",
                "先在授权管理处登记",
            )
        )
        return report

    # ---------- 安全兜底 ----------

    def safe_question(self, facts: dict, age_months: int, language: str) -> dict:
        """保证接地的最后一道闸：只用画面事实拼一个最短问题。"""
        band = age_band(age_months)
        limit = MAX_LEN[band]
        characters = [c for c in (facts.get("characters") or []) if c]
        objects = [o for o in (facts.get("objects") or []) if o]
        char = characters[0] if characters else ""
        obj = objects[0] if objects else ""
        if language == "en":
            text = f"What is {char or 'the little one'} doing?"
            qtype = "observe"
        elif band == "under3":
            text = f"这是{char}吗？" if char else "看看这是什么呀？"
            qtype = "observe"
        elif char:
            # 安全模板只能用画面已出现的主体，不能带“手”“脸”这类画外名词
            text = f"{char}在做什么呀？"
            qtype = "observe"
        elif obj:
            text = f"{obj}在哪里呀？"
            qtype = "observe"
        else:
            text = "你看到了什么呀？"
            qtype = "observe"
        return {
            "question": truncate(text, max(limit, 8)),
            "type": qtype,
            "expected": obj or char,
            "hint": "安全模板：只使用画面里的事实",
            "choices": [item for item in (char, obj) if item][:3],
            "language": language,
            "mode": "safe_template",
        }


class SelfChecker(ContentChecker):
    """闸门 + 记录：把"检查—重写—降级"的过程写进数据库，教师端可见。"""

    async def gate_summary(
        self,
        summary: dict,
        facts: dict,
        *,
        age_months: int,
        language: str,
        regenerate: Callable[[list[dict]], Awaitable[dict]],
        target: str = "",
    ) -> tuple[dict, dict]:
        report = self.check_summary(facts, summary, age_months=age_months, language=language)
        attempts = [{"stage": "initial", **report.to_dict()}]
        if report.verdict == "pass":
            return summary, self._gate_result("pass", report, attempts, 1, target)

        first_actions = list(report.actions)
        fixed = await regenerate(first_actions)
        again = self.check_summary(fixed, facts, age_months=age_months, language=language)
        attempts.append({"stage": "rewritten", **again.to_dict()})
        if again.verdict == "pass":
            return fixed, self._gate_result("rewrite", again, attempts, 2, target)

        # 仍不合格：退到规则版三层摘要（由事实直接拼，保证接地）
        fallback = nlg.fuse(facts, age_months, language)
        final = self.check_summary(fallback, facts, age_months=age_months, language=language)
        attempts.append({"stage": "safe_template", **final.to_dict()})
        return fallback, self._gate_result("block", final, attempts, 3, target)

    async def gate_question(
        self,
        question: dict,
        facts: dict,
        *,
        age_months: int,
        language: str,
        history: list[dict],
        regenerate: Callable[[list[str], bool], Awaitable[dict]],
        target: str = "",
        allowed_extra: set[str] | None = None,
    ) -> tuple[dict, dict]:
        report = self.check_question(
            question,
            facts,
            age_months=age_months,
            language=language,
            history=history,
            allowed_extra=allowed_extra,
        )
        attempts = [{"stage": "initial", "question": question.get("question"), **report.to_dict()}]
        if report.verdict == "pass":
            return question, self._gate_result("pass", report, attempts, 1, target)

        avoid = self._collect_avoid(report)
        need_different = any(action.get("avoid_repeat") for action in report.actions)
        regenerated = await regenerate(avoid, need_different)
        again = self.check_question(
            regenerated,
            facts,
            age_months=age_months,
            language=language,
            history=history,
            allowed_extra=allowed_extra,
        )
        attempts.append(
            {"stage": "rewritten", "question": regenerated.get("question"), **again.to_dict()}
        )
        if again.verdict == "pass":
            return regenerated, self._gate_result("rewrite", again, attempts, 2, target)

        safe = self.safe_question(facts, age_months, language)
        final = self.check_question(
            safe,
            facts,
            age_months=age_months,
            language=language,
            history=history,
            allowed_extra=allowed_extra,
        )
        attempts.append({"stage": "safe_template", "question": safe.get("question"), **final.to_dict()})
        return safe, self._gate_result("block", final, attempts, 3, target)

    def _collect_avoid(self, report: Report) -> list[str]:
        avoid: list[str] = []
        for action in report.actions:
            for item in action.get("avoid") or []:
                if item not in avoid:
                    avoid.append(str(item))
        return avoid

    def _gate_result(self, verdict: str, report: Report, attempts: list[dict], used: int, target: str) -> dict:
        result = {
            "verdict": verdict,
            "score": report.score,
            "attempts": used,
            "summary": report.summary_line(),
            "checks": [check.to_dict() for check in report.checks],
            "history": attempts,
            "checked_at": now(),
        }
        try:
            db.add_selfcheck(
                "content",
                target,
                verdict,
                report.score,
                result["checks"],
                [{"attempt": item["stage"], "verdict": item["verdict"]} for item in attempts],
            )
        except Exception:
            pass
        return result


# ---------------- 运行自检 ----------------


def _local_engine_status(name: str, settings) -> tuple[str, str, str]:
    """本机引擎能不能用：直接探依赖/模型/系统语音，不靠网络。"""
    if name == "ocr":
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            return "fail", "本机 OCR 没装", "pip install rapidocr-onnxruntime"
        return "ok", "RapidOCR 本机引擎（CPU，不联网）", ""
    if name == "asr":
        if not any(settings.asr_model_dir.rglob("model.bin")):
            return "fail", f"ASR 模型不在 {settings.asr_model_dir}", "运行 python scripts\\fetch_models.py --only asr 下载模型"
        try:
            subprocess.run([settings.ffmpeg_bin, "-version"], capture_output=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            return "fail", "找不到 ffmpeg（音频转码要用）", "安装 ffmpeg 并加进 PATH"
        return "ok", f"faster-whisper {settings.asr_model}（{settings.asr_device}/{settings.asr_compute_type}）", ""
    if name == "tts":
        try:
            import pythoncom
            import pyttsx3
        except ImportError:
            return "fail", "本机语音合成没装", "pip install pyttsx3"
        voice = ""
        try:
            pythoncom.CoInitialize()
            engine = pyttsx3.init()
            voices = [str(getattr(item, "name", "")) for item in engine.getProperty("voices") or []]
            engine.stop()
            chinese = [item for item in voices if any(mark in item.lower() for mark in ("zh", "chinese", "huihui"))]
            if not chinese:
                return "warn", f"没找到中文语音：{'、'.join(voices[:2]) or '无'}", "在 Windows 里装中文语音包"
            voice = chinese[0]
        except Exception as exc:  # noqa: BLE001
            return "fail", f"本机语音不可用：{str(exc)[:60]}", "检查 Windows 语音服务"
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        return "ok", f"Windows 本地语音 {voice}", ""
    return "warn", "本机模式只支持 OCR / ASR / TTS", "这个环节改成 http 接园内服务"


def run_system_check() -> dict:
    settings = get_settings()
    report = Report("system")
    checks = report.checks

    # 1. 数据库可读可写
    try:
        with db.tx() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS selfcheck_probe(x INTEGER)")
            conn.execute("INSERT INTO selfcheck_probe(x) VALUES (1)")
        checks.append(Check("db_writable", "记录可以正常保存", "ok"))
    except Exception as exc:
        checks.append(Check("db_writable", "记录可以正常保存", "fail", str(exc)[:80], "检查磁盘权限或联系管理员"))

    # 2. 存储可写 + 空间
    try:
        with tempfile.NamedTemporaryFile(dir=settings.data_dir, delete=True) as handle:
            handle.write(b"ok")
        usage = shutil.disk_usage(settings.data_dir)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1:
            checks.append(Check("disk", "存储空间充足", "warn", f"仅剩 {free_gb:.1f} GB", "清理旧备份或扩容"))
        else:
            checks.append(Check("disk", "存储空间充足", "ok", f"剩余 {free_gb:.0f} GB"))
    except Exception as exc:
        checks.append(Check("disk", "存储空间充足", "fail", str(exc)[:80], "检查数据目录权限"))

    # 3. 园内模型服务
    for name, mode in settings.model_modes.items():
        url = settings.model_endpoints[name]
        if mode == "stub":
            checks.append(
                Check(
                    f"model_{name}",
                    f"{name.upper()} 服务",
                    "warn",
                    "演示模式（确定性规则）",
                    "接入真实引擎后改成 local 或 http",
                )
            )
            continue
        if mode == "local":
            status, detail, action = _local_engine_status(name, settings)
            checks.append(Check(f"model_{name}", f"{name.upper()} 服务", status, detail, action))
            continue
        audit = guard.audit_url(url)
        if not audit.allowed:
            checks.append(Check(f"model_{name}", f"{name.upper()} 服务", "fail", audit.reason, "改成园内地址"))
            continue
        host = audit.host
        port = 80
        if ":" in url.split("//", 1)[-1]:
            port = int(url.split("//", 1)[-1].split("/")[0].split(":")[1])
        try:
            with socket.create_connection((host, port), timeout=1.5):
                checks.append(Check(f"model_{name}", f"{name.upper()} 服务", "ok", f"{host}:{port} 可达"))
        except OSError:
            checks.append(
                Check(f"model_{name}", f"{name.upper()} 服务", "fail", f"{host}:{port} 连不上", "确认 GPU 服务已启动")
            )

    # 4. 离线出站审计
    violations = [item for item in guard.audit_env_for_public_urls() if not item.allowed]
    if violations:
        hosts = sorted({item.host for item in violations})
        checks.append(
            Check("outbound", "无园外请求配置", "fail", f"发现园外地址：{'、'.join(hosts[:3])}", "改为园内地址")
        )
    else:
        checks.append(Check("outbound", "无园外请求配置", "ok"))

    # 5. 绘本库
    books = db.list_books()
    ready_pages = 0
    for book in books:
        ready_pages += sum(1 for page in db.list_pages(book["id"]) if page.get("status") == "ready")
    if not books:
        checks.append(Check("library", "绘本库可用", "warn", "还没有绘本", "先上传一本绘本"))
    elif ready_pages == 0:
        checks.append(Check("library", "绘本库可用", "warn", "绘本还没有可用的页", "在绘本库里补全页面"))
    else:
        checks.append(Check("library", "绘本库可用", "ok", f"{len(books)} 本 / {ready_pages} 页"))

    # 6. 监护授权
    children = db.list_children()
    missing = [child for child in children if not child["guardian_consent"]]
    if missing:
        checks.append(
            Check(
                "consent",
                "监护授权齐全",
                "warn",
                f"{len(missing)} 位幼儿未登记授权",
                "在授权管理处补登记（不影响共读，只影响周报）",
            )
        )
    else:
        checks.append(Check("consent", "监护授权齐全", "ok" if children else "warn", ""))

    # 7. 参数合理性
    threshold_ok = 0.5 <= settings.asr_confirm_threshold <= 0.95
    checks.append(
        Check(
            "asr_threshold",
            "语音确认阈值合理",
            "ok" if threshold_ok else "warn",
            f"当前 {settings.asr_confirm_threshold}",
            "建议 0.6-0.85",
        )
    )

    failed = [check for check in checks if check.status == "fail"]
    verdict = "block" if failed else "pass"
    result = {
        "scope": "system",
        "verdict": verdict,
        "score": report.score,
        "checks": [check.to_dict() for check in checks],
        "summary": (
            "有检查项未通过，请按提示处理" if failed else ("全部通过，可以开始共读" if not report.warnings else "可以开始共读，有 2 项提示")
        ),
        "modes": settings.model_modes,
        "demo_mode": settings.demo_mode,
        "checked_at": now(),
    }
    try:
        db.add_selfcheck("system", "system", verdict, report.score, result["checks"], [])
    except Exception:
        pass
    return result


def check_session(session: dict, turns: list[dict], advice_items: list[dict], memory_cleared: bool) -> dict:
    """会话后自检：记录完整性 + 建议可追溯 + 临时数据释放。"""
    checks: list[Check] = []
    if turns:
        checks.append(Check("turns_present", "本次有问答记录", "ok", f"{len(turns)} 页"))
    else:
        checks.append(Check("turns_present", "本次有问答记录", "warn", "本次没有完成任何一页", ""))

    no_checker = [turn for turn in turns if not turn.get("checker")]
    checks.append(
        Check(
            "turns_checked",
            "每条提问都过了自检",
            "fail" if no_checker else "ok",
            f"{len(no_checker)} 条没有自检记录" if no_checker else "",
            "检查编排链路",
        )
    )

    unevidenced = [item for item in advice_items if not item.get("evidence")]
    checks.append(
        Check(
            "advice_evidence",
            "教学建议全部可追溯",
            "fail" if unevidenced else "ok",
            f"{len(unevidenced)} 条没有依据" if unevidenced else "",
            "丢弃无依据建议",
        )
    )

    checks.append(
        Check(
            "memory_released",
            "临时数据已释放",
            "ok" if memory_cleared else "warn",
            "" if memory_cleared else "工作内存未清理",
            "结束会话后再试",
        )
    )

    answered = [turn for turn in turns if (turn.get("answer_text") or "").strip()]
    result = {
        "scope": "session",
        "verdict": "pass" if not [c for c in checks if c.status == "fail"] else "block",
        "score": round(max(0.0, 100 - 30 * len([c for c in checks if c.status == "fail"])), 1),
        "checks": [check.to_dict() for check in checks],
        "summary": f"{len(answered)}/{len(turns)} 页有回应" if turns else "本次未开始",
        "checked_at": now(),
    }
    try:
        db.add_selfcheck("session", session.get("id", ""), result["verdict"], result["score"], result["checks"], [])
    except Exception:
        pass
    return result