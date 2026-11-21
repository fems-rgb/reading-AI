"""把真实模型下载到本机（安装时联网一次，运行时完全离线）。

用法：
    python scripts/fetch_models.py                # 装全部（whisper small + qwen2.5:3b + qwen2.5vl:3b）
    python scripts/fetch_models.py --only asr     # 只下语音识别模型
    python scripts/fetch_models.py --check        # 只检查现有模型，不下载

下载源：
    - Whisper 走 HuggingFace 镜像（可用 HF_ENDPOINT 覆盖，默认 hf-mirror.com）
    - 语言/视觉模型走本机 Ollama（ollama pull）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_OLLAMA_MODELS = ("qwen2.5:3b", "qwen2.5vl:3b")

# 国内网络优先用镜像；部署时可用环境变量换成园内代理
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def _say(text: str) -> None:
    print(text, flush=True)


def fetch_whisper(name: str, target: Path) -> None:
    _say(f"[asr] 下载 faster-whisper 模型 {name} → {target}")
    from faster_whisper import WhisperModel

    target.mkdir(parents=True, exist_ok=True)
    WhisperModel(
        name,
        device="cpu",
        compute_type="int8",
        download_root=str(target),
        local_files_only=False,
    )
    if check_whisper(name, target):
        _say(f"[asr] 完成：{name}")
    else:
        raise SystemExit(f"[asr] 下载后仍找不到模型文件：{target}")


def check_whisper(name: str, target: Path) -> bool:
    if not target.exists():
        return False
    needle = f"faster-whisper-{name}"
    return any(path.name == "model.bin" for path in target.rglob("model.bin")) and any(
        needle in part for path in target.rglob("config.json") for part in path.parts
    )


def _ollama_bin() -> str:
    for candidate in ("ollama", str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe")):
        if not candidate:
            continue
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else "")
        if found:
            return found
    raise SystemExit("没有找到 ollama：先安装 https://ollama.com/download ，再重跑本脚本")


def ollama_models(binary: str) -> set[str]:
    try:
        out = subprocess.run([binary, "list"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.add(parts[0])
    return names


def fetch_ollama(models: tuple[str, ...]) -> None:
    binary = _ollama_bin()
    have = ollama_models(binary)
    for model in models:
        if model in have:
            _say(f"[llm] 已存在：{model}")
            continue
        _say(f"[llm] 拉取 {model}（首次约 2~3.5GB，按网速等待）")
        result = subprocess.run([binary, "pull", model])
        if result.returncode != 0:
            raise SystemExit(f"[llm] 拉取 {model} 失败，检查网络后重跑：ollama pull {model}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=("asr", "llm", "all"), default="all")
    parser.add_argument("--asr-model", default=os.environ.get("ASR_MODEL", "small"))
    parser.add_argument("--asr-dir", default=os.environ.get("ASR_MODEL_DIR", "models/whisper"))
    parser.add_argument("--llm-models", default=",".join(DEFAULT_OLLAMA_MODELS))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    asr_dir = Path(args.asr_dir)
    if not asr_dir.is_absolute():
        asr_dir = ROOT / asr_dir
    models = tuple(item.strip() for item in args.llm_models.split(",") if item.strip())

    if args.check:
        ok = True
        if args.only in ("asr", "all"):
            ready = check_whisper(args.asr_model, asr_dir)
            ok &= ready
            _say(f"[asr] {args.asr_model}: {'就绪' if ready else '缺少模型文件 ' + str(asr_dir)}")
        if args.only in ("llm", "all"):
            try:
                have = ollama_models(_ollama_bin())
            except SystemExit as exc:
                ok = False
                _say(f"[llm] {exc}")
                have = set()
            for model in models:
                ready = model in have
                ok &= ready
                _say(f"[llm] {model}: {'就绪' if ready else '未拉取'}")
        raise SystemExit(0 if ok else 1)

    if args.only in ("asr", "all"):
        if check_whisper(args.asr_model, asr_dir):
            _say(f"[asr] 已存在：{args.asr_model}")
        else:
            fetch_whisper(args.asr_model, asr_dir)
    if args.only in ("llm", "all"):
        fetch_ollama(models)
    _say("全部模型就绪：运行时不需要联网。")


if __name__ == "__main__":
    main()
