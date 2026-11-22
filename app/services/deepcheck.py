"""深度自检：真跑五个引擎的闭环，而不是只看"装没装"。

运行自检（selfcheck.run_system_check）回答的是"依赖在不在、端口通不通"；
可达不等于能用——模型文件损坏、Ollama 没拉全、语音包缺失，都会在
"端口可达"的自检里显示 ok，直到孩子坐下来的那一刻才失败。

这里现场生成一张测试卡片、合成一句语音，把五条链路真跑一遍：

    测试卡片 → OCR 认字（A1）→ VLM 描述画面（A2）
    固定问题 → LLM 回 JSON（A3/A4 的生成能力）
    一句话 → TTS 出声（A5）→ ASR 把同一段音频转写回来（A4）→ 与原文比对

每一项都记录实测耗时和输出，教师端运维面板与设备自检页共用这份结果。
"""

from __future__ import annotations

import threading
import time
from io import BytesIO

from .. import db
from ..config import get_settings
from ..utils import now, similarity
from . import speech
from .llm import LLMError, LocalLLM
from .vision import OcrService, VlmService

CARD_TEXT = "绘本共读"
SPOKEN = "小朋友你好，我们一起看绘本吧。"
DEEP_LOCK = threading.Lock()


def _font(size: int):
    from PIL import ImageFont

    for name in ("msyh.ttc", "simhei.ttf", "msyhbd.ttc"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def test_card() -> bytes:
    """一张绘本内页样的测试卡片：色块 + 短句，给 OCR/VLM 一个稳定的输入。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (960, 660), "#fffdf5")
    draw = ImageDraw.Draw(image)
    draw.rectangle([36, 36, 924, 624], outline="#e6d9bd", width=8)
    draw.ellipse([90, 90, 310, 310], fill="#f2c14e")  # 太阳
    draw.rectangle([80, 470, 880, 590], fill="#9ccb86")  # 草地
    draw.text((120, 380), CARD_TEXT, fill="#3a3226", font=_font(64))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _item(name: str, status: str, detail: str, fix: str = "", *, mode: str = "", ms: int = 0) -> dict:
    return {
        "name": name,
        "status": status,  # ok / warn / fail
        "detail": detail,
        "fix": fix,
        "mode": mode,
        "ms": ms,
    }


def _check_ocr(image: bytes) -> dict:
    started = time.perf_counter()
    try:
        result = OcrService().read_sync(image)
    except Exception as exc:  # noqa: BLE001 自检要接住一切，转成下一步
        return _item("OCR 识字", "fail", f"识别失败：{str(exc)[:90]}", "检查 rapidocr-onnxruntime 是否装好", ms=_ms(started))
    text = (result.text or "").replace("\n", "")
    if result.mode in {"stub", "library_cache"}:
        return _item("OCR 识字", "warn", f"当前是 {result.mode} 模式，没走真实引擎", "把 .env 的 OCR_MODE 改成 local", mode=result.mode, ms=_ms(started))
    if "绘本" in text or "共读" in text:
        return _item("OCR 识字", "ok", f"认出「{text[:24]}」", mode=result.mode, ms=_ms(started))
    return _item(
        "OCR 识字",
        "warn",
        f"引擎可用，但没认全卡片：{text[:30] or '空'}（置信度 {result.confidence:.2f}）",
        "换一张更清楚的测试图，或检查模型版本",
        mode=result.mode,
        ms=_ms(started),
    )


def _check_vlm(image: bytes) -> dict:
    started = time.perf_counter()
    try:
        result = VlmService().analyze_sync(image)
    except Exception as exc:  # noqa: BLE001
        return _item("VLM 看画面", "fail", f"描述失败：{str(exc)[:90]}", "确认 Ollama 已启动、VLM_MODEL 已下载", ms=_ms(started))
    if result.mode == "stub" or result.degraded:
        return _item("VLM 看画面", "warn", "当前是 stub 模式，没走真实视觉模型", "把 .env 的 VLM_MODE 改成 http 并指到园内地址", mode=result.mode, ms=_ms(started))
    seen = "、".join((result.characters or []) + (result.objects or [])) or result.scene or "没写出具体物体"
    return _item("VLM 看画面", "ok", f"{result.mode}，看到：{seen[:40]}", mode=result.mode, ms=_ms(started))


def _check_llm() -> dict:
    started = time.perf_counter()
    llm = LocalLLM()
    if not llm.enabled:
        return _item("LLM 生成", "warn", "当前是 stub 模式（规则生成），没调模型", "把 .env 的 LLM_MODE 改成 http", ms=_ms(started))
    try:
        data, meta = llm.chat_json(
            "你在做设备自检，只输出 JSON。",
            '只输出这一行 JSON，不要解释：{"answer":"好"}',
            temperature=0.0,
        )
    except LLMError as exc:
        return _item("LLM 生成", "fail", f"调用失败：{str(exc)[:90]}", "确认 Ollama 已启动、LLM_MODEL 已下载", ms=_ms(started))
    if not isinstance(data, dict) or not data:
        return _item("LLM 生成", "warn", f"模型可用，但没按 JSON 返回：{str(data)[:40]}", "换参数更大的模型，或检查提示词", mode=str(meta.get("model") or ""), ms=_ms(started))
    answer = str(data.get("answer") or "").strip()
    return _item("LLM 生成", "ok", f"{meta.get('model')}，回答「{answer[:12] or '（空）'}」", mode=str(meta.get("model") or ""), ms=_ms(started))


def _check_tts() -> tuple[dict, bytes]:
    started = time.perf_counter()
    try:
        result = speech.synthesize(SPOKEN)
    except Exception as exc:  # noqa: BLE001
        return _item("TTS 出声", "fail", f"合成失败：{str(exc)[:90]}", "检查 Windows 中文语音包与 pyttsx3", ms=_ms(started)), b""
    mode = str(result.get("mode") or "")
    if mode == "stub":
        return _item("TTS 出声", "warn", "当前是提示音模式，没有真人声", "把 .env 的 TTS_MODE 改成 local", mode=mode, ms=_ms(started)), b""
    name = str(result.get("url") or "").rsplit("/", 1)[-1]
    path = get_settings().tts_dir / name
    audio = path.read_bytes() if path.exists() else b""
    if len(audio) < 2048:
        return _item("TTS 出声", "fail", f"音频只有 {len(audio)} 字节", "检查 Windows 中文语音包是否完整", mode=mode, ms=_ms(started)), b""
    detail = f"{mode}，{result.get('duration')} 秒，{len(audio) // 1024} KB"
    return _item("TTS 出声", "ok", detail, mode=mode, ms=_ms(started)), audio


def _check_asr(audio: bytes) -> dict:
    started = time.perf_counter()
    if not audio:
        return _item("ASR 转写", "warn", "上一步没生成音频，跳过回放", "先修好 TTS", ms=_ms(started))
    try:
        result = speech.transcribe(audio)
    except Exception as exc:  # noqa: BLE001
        return _item("ASR 转写", "fail", f"转写失败：{str(exc)[:90]}", "检查 faster-whisper 与 ffmpeg", ms=_ms(started))
    mode = str(result.get("mode") or "")
    if mode == "stub":
        return _item("ASR 转写", "warn", "当前是脚本样例模式", "把 .env 的 ASR_MODE 改成 local", mode=mode, ms=_ms(started))
    if mode == "unavailable":
        return _item("ASR 转写", "fail", str(result.get("error") or "模型不可用")[:90], "运行 python scripts\\fetch_models.py --only asr", mode=mode, ms=_ms(started))
    text = str(result.get("text") or "").strip()
    if not text:
        return _item("ASR 转写", "fail", "模型在，但这段音频没转出文字", "检查音频格式与音量", mode=mode, ms=_ms(started))
    score = similarity(text, SPOKEN)
    if "绘本" in text or score >= 0.3:
        return _item("ASR 转写", "ok", f"转回「{text[:24]}」，相似度 {score:.2f}", mode=mode, ms=_ms(started))
    return _item("ASR 转写", "warn", f"转回「{text[:24]}」，与原文差得多（{score:.2f}）", "检查音响音量，或换更清楚的中文语音", mode=mode, ms=_ms(started))


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def run_deep_check() -> dict:
    """同步执行（在 API 层丢线程池）。同一时间只允许一次，避免抢 GPU。"""
    if not DEEP_LOCK.acquire(blocking=False):
        return {
            "scope": "deep",
            "verdict": "busy",
            "score": 0.0,
            "checks": [],
            "summary": "上一次深度自检还在跑，等它结束再来一次",
            "checked_at": now(),
        }
    try:
        started = time.perf_counter()
        try:
            image = test_card()
            card_error = ""
        except Exception as exc:  # noqa: BLE001
            image = b""
            card_error = str(exc)[:80]
        if image:
            checks = [_check_ocr(image), _check_vlm(image)]
        else:
            checks = [
                _item("OCR 识字", "fail", f"测试卡片生成失败：{card_error}", "检查 Pillow 与中文字体"),
                _item("VLM 看画面", "fail", f"测试卡片生成失败：{card_error}", "检查 Pillow 与中文字体"),
            ]
        checks.append(_check_llm())
        tts, audio = _check_tts()
        checks.append(tts)
        checks.append(_check_asr(audio))

        failed = [item for item in checks if item["status"] == "fail"]
        warned = [item for item in checks if item["status"] == "warn"]
        score = round(max(0.0, 100.0 - 30 * len(failed) - 8 * len(warned)), 1)
        total_ms = _ms(started)
        if failed:
            summary = f"{len(failed)} 个引擎没跑通，先按提示修好再给孩子用"
        elif warned:
            summary = f"链路都跑通了，有 {len(warned)} 项是提示，共 {total_ms / 1000:.1f} 秒"
        else:
            summary = f"五个引擎全部真跑通，共 {total_ms / 1000:.1f} 秒"
        result = {
            "scope": "deep",
            "verdict": "block" if failed else "pass",
            "score": score,
            "checks": checks,
            "summary": summary,
            "total_ms": total_ms,
            "modes": get_settings().model_modes,
            "checked_at": now(),
        }
        try:
            db.add_selfcheck("deep", "system", result["verdict"], score, checks, [])
        except Exception:  # noqa: BLE001 自检结果落库失败不影响返回值
            pass
        return result
    finally:
        DEEP_LOCK.release()
