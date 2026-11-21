"""本地启动器：python run.py

默认只监听 127.0.0.1:8000（园内网用 HOST=0.0.0.0 打开，
但出站访问仍然只允许园内地址，见 app/guard.py）。

启动前把五个引擎挨个探一遍：装没装、模型在不在、Ollama 通不通，
用中文写清下一步，老师不用去翻日志。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import urllib.request
from urllib.parse import urlparse

import uvicorn

from app.config import get_settings


def _port_open(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _ollama_models(url: str) -> set[str] | None:
    """网址是本机 Ollama（11434）时，返回已有模型名；其他服务返回 None。"""
    parsed = urlparse(url)
    if not parsed.hostname or parsed.port != 11434:
        return None
    base = f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {str(item.get("name", "")) for item in data.get("models", [])}
    except Exception:  # noqa: BLE001
        return None


def _model_missing(models: set[str] | None, model: str) -> bool:
    """能拿到模型清单且里头没有这个模型 → True；拿不到就不猜。"""
    if models is None:
        return False
    name = model if ":" in model else model + ":latest"
    return name not in models


def _engine_lines(settings) -> list[tuple[str, str, str]]:
    """(状态, 环节, 说明)。ok / warn / fail 三档，warn 也算能用。"""
    rows: list[tuple[str, str, str]] = []

    if settings.ocr_mode == "local":
        try:
            import rapidocr_onnxruntime  # noqa: F401

            rows.append(("ok", "A1 文字识别", "RapidOCR（本机 CPU）"))
        except ImportError:
            rows.append(("fail", "A1 文字识别", "没装：pip install rapidocr-onnxruntime"))
    elif settings.ocr_mode == "http":
        reachable = _port_open(settings.ocr_url)
        rows.append(("ok" if reachable else "fail", "A1 文字识别", settings.ocr_url))
    else:
        rows.append(("warn", "A1 文字识别", "stub：不认字，只走规则"))

    for name, mode, url, model in (
        ("A2 看图", settings.vlm_mode, settings.vlm_url, settings.vlm_model),
        ("A3/A4/A7/A10 语言", settings.llm_mode, settings.llm_url, settings.llm_model),
    ):
        if mode == "http":
            reachable = _port_open(url)
            models = _ollama_models(url) if reachable else None
            detail = f"{model} @ {url}" + ("（本机 Ollama）" if models is not None else "")
            if not reachable:
                rows.append(("fail", name, f"连不上 {url}：确认 Ollama 在运行"))
            elif _model_missing(models, model):
                rows.append(("fail", name, f"模型没下载：ollama pull {model}（或跑 scripts\\fetch_models.py）"))
            else:
                rows.append(("ok", name, detail))
        else:
            rows.append(("warn", name, "stub：用规则文案，不是真模型"))

    if settings.asr_mode == "local":
        if not settings.asr_model_dir.exists():
            rows.append(("fail", "A6 语音识别", f"模型目录不存在：{settings.asr_model_dir}"))
        else:
            has_model = any(settings.asr_model_dir.rglob("model.bin"))
            ffmpeg = shutil.which(settings.ffmpeg_bin)
            if not has_model:
                rows.append(("fail", "A6 语音识别", "缺模型：python scripts\\fetch_models.py --only asr"))
            elif not ffmpeg:
                rows.append(("fail", "A6 语音识别", "缺 ffmpeg：winget install Gyan.FFmpeg"))
            else:
                rows.append(("ok", "A6 语音识别", f"faster-whisper {settings.asr_model}（CPU）"))
    elif settings.asr_mode == "http":
        reachable = _port_open(settings.asr_url)
        rows.append(("ok" if reachable else "fail", "A6 语音识别", settings.asr_url))
    else:
        rows.append(("warn", "A6 语音识别", "stub：转写是脚本样例"))

    if settings.tts_mode == "local":
        if os.name != "nt":
            rows.append(("fail", "A5 语音合成", "本机 SAPI 只在 Windows 上可用"))
        else:
            rows.append(("ok", "A5 语音合成", "Windows 系统语音（中文女声）"))
    elif settings.tts_mode == "http":
        reachable = _port_open(settings.tts_url)
        rows.append(("ok" if reachable else "fail", "A5 语音合成", settings.tts_url))
    else:
        rows.append(("warn", "A5 语音合成", "stub：播放的是提示音"))
    return rows


def main() -> None:
    settings = get_settings()
    print("=" * 64)
    print("绘本阅读陪伴 AI 智能体")
    print(f"  地址      : http://{settings.host}:{settings.port}")
    print(f"  教师端    : http://{settings.host}:{settings.port}/teacher")
    print(f"  设备自检  : http://{settings.host}:{settings.port}/device-check")
    print(f"  数据目录  : {settings.data_dir}")
    print(f"  口令      : 见 .env（TEACHER_PIN / ADMIN_PIN），不在控制台回显")
    print(f"  演示模式  : {'是（有环节还没接真实引擎）' if settings.demo_mode else '否（五个引擎都是真实的）'}")
    print("-" * 64)
    rows = _engine_lines(settings)
    for status, name, detail in rows:
        mark = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}[status]
        print(f"  {mark} {name:18s} {detail}")
    if any(status == "fail" for status, _, _ in rows):
        print("  ↑ 有引擎没准备好：先跑 powershell -ExecutionPolicy Bypass -File scripts\\install.ps1")
    print("=" * 64)

    if settings.demo_mode:
        print("提示：界面会显著标“演示模式”，不会把规则结果当成模型结果。")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
