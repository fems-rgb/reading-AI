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
import os
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

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
    if settings.tts_mode == "local":
        return _synthesize_local(text, voice)
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


_TTS_LOCK = threading.Lock()


def _synthesize_local(text: str, voice: str) -> dict:
    """本机 SAPI 中文女声：不联网、不占显卡，园里电脑装好系统就能发声。"""
    settings = get_settings()
    key = short_hash(f"local:{voice}:{text}")
    path = settings.tts_dir / f"{key}.wav"
    if not path.exists():
        with _TTS_LOCK:
            if not path.exists():
                _sapi_save(text, voice, path)
    audio = path.read_bytes() if path.exists() else b""
    if not audio:
        raise RuntimeError("本机语音合成没有生成音频：检查 Windows 中文语音包")
    return {
        "url": f"/media/tts/{key}.wav",
        "duration": _wav_duration(audio),
        "mode": "sapi5",
        "text": text,
    }


def _sapi_save(text: str, voice: str, path: Path) -> None:
    try:
        import pythoncom
        import pyttsx3
    except ImportError as exc:
        raise RuntimeError("本机 TTS 没装：pip install pyttsx3") from exc
    pythoncom.CoInitialize()
    engine = pyttsx3.init()
    try:
        chosen = _pick_voice(engine, voice)
        if chosen:
            engine.setProperty("voice", chosen)
        engine.setProperty("rate", 150)  # 比成人默认语速慢一点，孩子听得清
        engine.save_to_file(text, str(path))
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass
        pythoncom.CoUninitialize()


def _pick_voice(engine, wanted: str) -> str:
    """按名字选；选不到就用第一个中文语音（Windows 自带 Huihui）。"""
    voices = list(engine.getProperty("voices") or [])
    wanted = (wanted or "").strip().lower()
    if wanted and wanted not in {"zh-cn-kids", "kids"}:
        for candidate in voices:
            blob = f"{getattr(candidate, 'name', '')} {getattr(candidate, 'id', '')}".lower()
            if wanted in blob:
                return candidate.id
    for candidate in voices:
        blob = f"{getattr(candidate, 'name', '')} {getattr(candidate, 'id', '')} {getattr(candidate, 'languages', '')}".lower()
        if any(mark in blob for mark in ("zh", "chinese", "huihui")):
            return candidate.id
    return voices[0].id if voices else ""


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
    if settings.asr_mode == "local":
        return _transcribe_local(audio, language)
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


_ASR_LOCK = threading.Lock()
_ASR_MODEL = None


def _whisper():
    """faster-whisper 按进程只加载一次：模型必须在本地，运行时不去下载。"""
    global _ASR_MODEL
    if _ASR_MODEL is None:
        with _ASR_LOCK:
            if _ASR_MODEL is None:
                settings = get_settings()
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise RuntimeError("本机 ASR 没装：pip install faster-whisper") from exc
                _ASR_MODEL = WhisperModel(
                    settings.asr_model,
                    device=settings.asr_device,
                    compute_type=settings.asr_compute_type,
                    download_root=str(settings.asr_model_dir),
                    local_files_only=True,
                )
    return _ASR_MODEL


def _decode_16k(audio: bytes, ffmpeg: str) -> np.ndarray:
    """浏览器给的 webm/ogg/wav 统一转 16k 单声道，再交给 whisper。"""
    with tempfile.TemporaryDirectory(prefix="reading-ai-") as folder:
        source = Path(folder) / "input.bin"
        source.write_bytes(audio)
        target = Path(folder) / "asr.wav"
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                 "-ac", "1", "-ar", "16000", "-f", "wav", str(target)],
                check=True, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("音频转码失败：检查 ffmpeg 是否装好") from exc
        with wave.open(str(target)) as handle:
            raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe_local(audio: bytes, language: str) -> dict:
    settings = get_settings()
    try:
        model = _whisper()
        samples = _decode_16k(audio, settings.ffmpeg_bin)
    except Exception as exc:
        return {
            "text": "", "confidence": 0.0, "mode": "unavailable",
            "language": language, "error": str(exc)[:120],
        }
    with _ASR_LOCK:
        segments, _info = model.transcribe(
            samples,
            language=language,
            vad_filter=True,
            beam_size=5,
            # 中文绘本场景：明确要求简体，避免输出繁体/英文
            initial_prompt="以下是普通话的句子，请用简体中文转写。",
        )
        segments = list(segments)
    text = "".join(segment.text for segment in segments).strip()
    logs = [segment.avg_logprob for segment in segments if segment.avg_logprob is not None]
    confidence = math.exp(sum(logs) / len(logs)) if logs else 0.0
    return {
        "text": text,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "mode": f"faster-whisper:{settings.asr_model}",
        "language": language,
    }