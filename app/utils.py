"""小工具：时间、相似度、截断、哈希。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher


def now() -> str:
    """本地时间，精确到秒，直接写库（离线单机，不需要时区换算）。"""
    return datetime.now().isoformat(timespec="seconds")


def today_start() -> str:
    return datetime.now().strftime("%Y-%m-%dT00:00:00")


def days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s，。？！、,.?!~·…\"'“”‘’（）()\-]", "", text)


def similarity(a: str, b: str) -> float:
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def short_hash(data: bytes | str, length: int = 12) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:length]


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?；;]\s*", (text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def age_band(age_months: int) -> str:
    """规范 §5.6 的分年龄段策略键。"""
    if age_months < 36:
        return "under3"
    if age_months < 48:
        return "3_4"
    return "4_6"


def age_label(age_months: int) -> str:
    years, months = divmod(max(age_months, 0), 12)
    if years <= 0:
        return f"{months} 个月"
    return f"{years} 岁 {months} 个月" if months else f"{years} 岁"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))