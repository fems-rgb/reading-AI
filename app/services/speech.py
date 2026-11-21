"""A5/A6：语音合成与童声识别。

stub 模式不假装"会说话"：TTS 生成时长与文本匹配的提示音（保证播放/打断
时序可测），ASR 返回脚本化转写并在界面标注"演示"。接入园内 FunASR / TTS
后把 *_MODE 改成 http 即可，上层编排与自检逻辑不变。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import time
import urllib.request
import wave
from pathlib import Path

from .. import guard
from ..config import get_settings
from ..utils import short_hash

SAMPLE_RATE = 16000

# 播放缓存的保留期：同一句话会按文本哈希复用，但问答文本里可能带有孩子原话，
# 所以不能无限期留在磁盘上。启动时清一次超期的，会话结束时清自己产生的。
KEEP_DAYS = 7

# 演示用"孩子会说的话"：含低置信度样本，用来走"你是不是说…"确认式追问
DEMO_ANSWERS: tuple[tuple[str, float], ...] = (
    ("小兔子拿着风筝", 0.91),
    ("是风筝", 0.86),
    ("不知道", 0.9),
    ("为什么呀", 0.88),
    ("小熊摔倒了", 0.71),
    ("在草地上跑", 0.93),
)


def _tone_wav(text: str) -> bytes:
    """按字数生成一段柔和的提示音，时长与真人朗读接近。"""
    characters = [ch for ch in text if not ch.isspace()]
    seconds = min(0.55 + 0.17 * len(characters), 10.0)
    frames = bytearray()
    total_samples = int(SAMPLE_RATE * seconds)
    for index in range(total_samples):
        t = index / SAMPLE_RATE
        slot = int(t / 0.17)
        char = characters[slot % len(characters)] if characters else "·"
        base = 300 + (ord(char) % 90)
        envelope = min(1.0, t / 0.04, (seconds - t) / 0.08)
        value = (
            math.sin(2 * math.pi * base * t) * 0.55
            + math.sin(2 * math.pi * base * 1.5 * t) * 0.2
        )
        sample = int(12000 * envelope * value * 0.35)
        frames += struct.pack("<h", max(-32767, min(32767, sample)))
    buffer = __import__("io").BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def synthesize(text: str, voice: str = "") -> dict:
    settings = get_settings()
    text = (text or "").strip()
    voice = voice or settings.tts_voice
    if not text:
        return {"url": "", "duration": 0.0, "mode": "none"}
    if settings.tts_mode == "http":
        return _synthesize_http(text, voice)
    key = short_hash(f"{voice}:{text}")
    path: Path = settings.tts_dir / f"{key}.wav"
    if not path.exists():
        path.write_bytes(_tone_wav(text))
    duration = round(min(0.55 + 0.17 * len(text), 10.0), 2)
    return {
        "url": f"/media/tts/{key}.wav",
        "duration": duration,
        "mode": "stub",
        "text": text,
    }


def _synthesize_http(text: str, voice: str) -> dict:
    settings = get_settings()
    payload = json.dumps({"text": text, "voice": voice, "format": "wav"}).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.tts_url}/synthesize",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with guard.safe_urlopen(request, timeout=30) as response:
        audio = response.read()
    key = short_hash(f"http:{voice}:{text}")
    path = settings.tts_dir / f"{key}.wav"
    path.write_bytes(audio)
    duration = _wav_duration(audio)
    return {"url": f"/media/tts/{key}.wav", "duration": duration, "mode": "http", "text": text}


def _wav_duration(audio: bytes) -> float:
    try:
        import io

        with wave.open(io.BytesIO(audio)) as handle:
            return round(handle.getnframes() / float(handle.getframerate() or 1), 2)
    except Exception:
        return 0.0


def key_from_url(url: str) -> str:
    name = (url or "").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".wav") else ""


def forget(urls, keep: set[str] | None = None) -> int:
    """删掉这些播放缓存；keep 里的（别的并行会话还在用）跳过。"""
    removed = 0
    keep = keep or set()
    for url in urls or ():
        if url in keep:
            continue
        key = key_from_url(url)
        if not key:
            continue
        path = get_settings().tts_dir / f"{key}.wav"
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def prune_old(days: int = KEEP_DAYS) -> int:
    """启动时清一次：上次异常退出 / 服务重启留下的超期缓存。"""
    cutoff = time.time() - days * 86400
    removed = 0
    for path in get_settings().tts_dir.glob("*.wav"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def transcribe(audio: bytes, language: str = "zh") -> dict:
    settings = get_settings()
    if not audio:
        return {"text": "", "confidence": 0.0, "mode": "empty"}
    if settings.asr_mode == "http":
        return _transcribe_http(audio, language)
    digest = hashlib.sha256(audio).hexdigest()
    text, confidence = DEMO_ANSWERS[int(digest[:8], 16) % len(DEMO_ANSWERS)]
    return {"text": text, "confidence": confidence, "mode": "stub", "language": language}


def _transcribe_http(audio: bytes, language: str) -> dict:
    settings = get_settings()
    payload = json.dumps(
        {
            "audio_b64": base64.b64encode(audio).decode("ascii"),
            "language": language,
            "sample_rate": SAMPLE_RATE,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.asr_url}/transcribe",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with guard.safe_urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {
        "text": data.get("text", ""),
        "confidence": float(data.get("confidence") or 0.0),
        "mode": "funasr",
        "language": language,
    }