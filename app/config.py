"""运行时配置：只读环境变量 + .env，不引入额外依赖。

设计原则（规范 §6）：默认完全离线。所有模型地址都必须在园内网，
由 guard.py 在每次出站前强制校验，配置写错也不会把数据送出去。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env_file() -> None:
    """极简 .env 解析：不覆盖已存在的真实环境变量。"""
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file()


def _text(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _number(name: str, default: float) -> float:
    try:
        return float(_text(name, str(default)))
    except ValueError:
        return default


def _whole(name: str, default: int) -> int:
    return int(_number(name, default))


def _flag(name: str, default: bool) -> bool:
    return _text(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    db_path: Path
    library_dir: Path
    tts_dir: Path

    offline_only: bool
    allowed_hosts: tuple[str, ...]

    host: str
    port: int
    secret_key: str
    teacher_pin: str
    admin_pin: str

    question_delay_seconds: float
    listen_timeout_seconds: float
    teacher_timeout_seconds: float
    page_limit_per_session: int
    daily_call_limit: int
    asr_confirm_threshold: float
    keep_page_image: bool

    ocr_mode: str
    vlm_mode: str
    llm_mode: str
    asr_mode: str
    tts_mode: str

    ocr_url: str
    vlm_url: str
    vlm_model: str
    llm_url: str
    llm_model: str
    asr_url: str
    tts_url: str
    tts_voice: str

    @property
    def demo_mode(self) -> bool:
        """任一环节还是确定性 stub，界面上就要显著提示"演示模式"。"""
        return "stub" in {self.ocr_mode, self.vlm_mode, self.llm_mode, self.asr_mode, self.tts_mode}

    @property
    def model_modes(self) -> dict[str, str]:
        return {
            "ocr": self.ocr_mode,
            "vlm": self.vlm_mode,
            "llm": self.llm_mode,
            "asr": self.asr_mode,
            "tts": self.tts_mode,
        }

    @property
    def model_endpoints(self) -> dict[str, str]:
        return {
            "ocr": self.ocr_url,
            "vlm": self.vlm_url,
            "llm": self.llm_url,
            "asr": self.asr_url,
            "tts": self.tts_url,
        }

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.library_dir, self.tts_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(_text("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    allowed = tuple(
        item.strip().lower()
        for item in _text("ALLOWED_HOSTS").split(",")
        if item.strip()
    )
    settings = Settings(
        root=ROOT,
        data_dir=data_dir,
        db_path=data_dir / "app.db",
        library_dir=data_dir / "library",
        tts_dir=data_dir / "tts",
        offline_only=_flag("OFFLINE_ONLY", True),
        allowed_hosts=allowed,
        host=_text("HOST", "127.0.0.1"),
        port=_whole("PORT", 8000),
        secret_key=_text("SECRET_KEY") or secrets.token_hex(32),
        teacher_pin=_text("TEACHER_PIN", "2026"),
        admin_pin=_text("ADMIN_PIN", "8642"),
        question_delay_seconds=_number("QUESTION_DELAY_SECONDS", 8),
        listen_timeout_seconds=_number("LISTEN_TIMEOUT_SECONDS", 20),
        teacher_timeout_seconds=_number("TEACHER_TIMEOUT_SECONDS", 15),
        page_limit_per_session=_whole("PAGE_LIMIT_PER_SESSION", 60),
        daily_call_limit=_whole("DAILY_CALL_LIMIT", 3000),
        asr_confirm_threshold=_number("ASR_CONFIRM_THRESHOLD", 0.75),
        keep_page_image=_flag("KEEP_PAGE_IMAGE", False),
        ocr_mode=_text("OCR_MODE", "stub").lower(),
        vlm_mode=_text("VLM_MODE", "stub").lower(),
        llm_mode=_text("LLM_MODE", "stub").lower(),
        asr_mode=_text("ASR_MODE", "stub").lower(),
        tts_mode=_text("TTS_MODE", "stub").lower(),
        ocr_url=_text("OCR_URL", "http://127.0.0.1:9101").rstrip("/"),
        vlm_url=_text("VLM_URL", "http://127.0.0.1:9102/v1").rstrip("/"),
        vlm_model=_text("VLM_MODEL", "qwen3-vl-8b-instruct"),
        llm_url=_text("LLM_URL", "http://127.0.0.1:9103/v1").rstrip("/"),
        llm_model=_text("LLM_MODEL", "qwen3-8b-instruct"),
        asr_url=_text("ASR_URL", "http://127.0.0.1:9104").rstrip("/"),
        tts_url=_text("TTS_URL", "http://127.0.0.1:9105").rstrip("/"),
        tts_voice=_text("TTS_VOICE", "zh-CN-kids"),
    )
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    """测试用：让下次 get_settings() 重新读取环境变量。"""
    get_settings.cache_clear()