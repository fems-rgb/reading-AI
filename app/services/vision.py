"""A1 文字识别 / A2 图像理解 / 拍摄质量检测。

分工严格按规范 §5.1：OCR 只负责"字"，VLM 只负责"画面语义"，
两者并行，任何一层失败都不阻断流程（降级，而不是整页失败）。
"""

from __future__ import annotations

import base64
import io
import json
import threading
import urllib.request
from dataclasses import dataclass, field

from PIL import Image, ImageFilter, ImageStat

from .. import guard
from ..config import get_settings

# 阈值按园区 720p 拍摄实测可调；调高=更宽松
BLUR_THRESHOLD = 18.0
DARK_THRESHOLD = 45.0
BRIGHT_THRESHOLD = 232.0
GLARE_RATIO = 0.32
MIN_EDGE = 400

# 本机 OCR 引擎按进程只建一次（加载模型约 1 秒），多线程下共用
_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()


def _rapidocr():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


@dataclass
class QualityReport:
    ok: bool
    code: str = "ok"
    hint: str = ""
    metrics: dict = field(default_factory=dict)
    actions: list[str] = field(default_factory=lambda: ["retake", "skip", "teacher_talk"])

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "metrics": self.metrics,
            "actions": self.actions,
        }


def assess_quality(image_bytes: bytes) -> QualityReport:
    """模糊 / 过暗 / 过曝 / 反光 / 尺寸过小，拍糊了就不进识别链路。"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        return QualityReport(False, "unreadable", "这张照片没有打开，再拍一次好吗？", {})

    rgb = image.convert("RGB")
    width, height = rgb.size
    gray = rgb.convert("L")
    scale = min(1.0, 640 / max(width, height))
    probe = gray.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    stats = ImageStat.Stat(probe)
    brightness = float(stats.mean[0])
    contrast = float(stats.stddev[0])
    edges = probe.filter(ImageFilter.FIND_EDGES)
    sharpness = float(ImageStat.Stat(edges).var[0])

    histogram = probe.histogram()
    total = sum(histogram) or 1
    glare = sum(histogram[252:]) / total

    metrics = {
        "width": width,
        "height": height,
        "brightness": round(brightness, 1),
        "contrast": round(contrast, 1),
        "sharpness": round(sharpness, 1),
        "glare": round(glare, 3),
    }

    if min(width, height) < MIN_EDGE:
        return QualityReport(False, "too_small", "离得太远啦，把书靠近一点再拍。", metrics)
    if brightness < DARK_THRESHOLD:
        return QualityReport(False, "too_dark", "这里有点暗，把书挪到亮一点的地方好吗？", metrics)
    if sharpness < BLUR_THRESHOLD:
        return QualityReport(False, "blurry", "这张有点花，再拍一次好吗？", metrics)
    if brightness > BRIGHT_THRESHOLD or glare > GLARE_RATIO:
        return QualityReport(False, "glare", "灯太亮啦，换个角度再拍一次。", metrics)
    if contrast < 12:
        return QualityReport(False, "low_contrast", "这张看不清，再拍一次好吗？", metrics)
    return QualityReport(True, "ok", "", metrics, actions=[])


@dataclass
class OcrResult:
    text: str
    lines: list[dict]
    confidence: float
    mode: str
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "lines": self.lines,
            "confidence": round(self.confidence, 3),
            "mode": self.mode,
            "degraded": self.degraded,
        }


class OcrService:
    """A1。库内页面直接用预识别缓存；未入库页面才现场识别。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    def read_sync(self, image_bytes: bytes, cached: dict | None = None) -> OcrResult:
        if cached and (cached.get("raw_text") or "").strip():
            return OcrResult(
                text=cached["raw_text"],
                lines=self._split_lines(cached["raw_text"]),
                confidence=float(cached.get("ocr_confidence") or 0.95),
                mode="library_cache",
            )
        if self.settings.ocr_mode == "local":
            return self._read_local(image_bytes)
        if self.settings.ocr_mode != "http":
            return OcrResult("", [], 0.0, "stub", degraded=True)
        return self._read_http(image_bytes)

    def _read_local(self, image_bytes: bytes) -> OcrResult:
        """RapidOCR（onnxruntime，CPU）在进程里识字：完全离线，不占显卡。"""
        try:
            engine = _rapidocr()
            result, _elapse = engine(image_bytes)
        except ImportError as exc:
            raise RuntimeError("本机 OCR 没装：pip install rapidocr-onnxruntime") from exc
        except Exception as exc:
            raise RuntimeError(f"本机 OCR 识别失败：{exc}") from exc
        lines = [
            {"text": str(item[1]), "confidence": round(float(item[2]), 3)}
            for item in (result or [])
            if len(item) >= 3 and str(item[1]).strip()
        ]
        confidence = sum(line["confidence"] for line in lines) / len(lines) if lines else 0.0
        return OcrResult("\n".join(line["text"] for line in lines), lines, confidence, "rapidocr")

    def _read_http(self, image_bytes: bytes) -> OcrResult:
        payload = json.dumps(
            {"image_b64": base64.b64encode(image_bytes).decode("ascii"), "lang": "ch"}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.settings.ocr_url}/ocr",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with guard.safe_urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        lines = data.get("lines") or []
        text = data.get("text") or "".join(line.get("text", "") for line in lines)
        confidence = float(data.get("confidence") or 0.0)
        return OcrResult(text, lines, confidence, "paddleocr")

    @staticmethod
    def _split_lines(text: str) -> list[dict]:
        return [{"text": line, "confidence": 1.0} for line in text.splitlines() if line.strip()]


@dataclass
class VlmResult:
    characters: list[str]
    objects: list[str]
    scene: str
    relations: list[str]
    mood: str
    mode: str
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "characters": self.characters,
            "objects": self.objects,
            "scene": self.scene,
            "relations": self.relations,
            "mood": self.mood,
            "mode": self.mode,
            "degraded": self.degraded,
        }


def _loose_json(text: str) -> dict:
    import re

    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"视觉模型没有返回可解析的 JSON：{text[:120]}")


# 名词短语里不该出现“在/正在”这种谓语标记；只拦这个，避免误伤“飞机/笑容”
_LABEL_SENTENCE_HINTS = ("在", "正在")


def _short_labels(items: list, limit: int = 10) -> list[str]:
    """characters/objects 只收短名词短语：动作句（含“在”、过长、带标点）一律丢弃。

    实测空白页会让视觉模型编出“一只小猫在草地上奔跑”，这种条目一旦进
    了 facts 就会被当成画面事实，所以必须在入口拦掉。
    """
    kept: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if not text or len(text) > limit or any(ch in text for ch in "。！？；，"):
            continue
        if any(hint in text for hint in _LABEL_SENTENCE_HINTS):
            continue
        kept.append(text)
    return kept


class VlmService:
    """A2。只输出“能看见”的东西，不写情节推断。"""

    PROMPT = (
        "你是绘本画面标注员。只描述这张图里直接可见的内容，不要推测情节、心理或画外信息。"
        "看不清、空白或模糊时，characters 与 objects 返回空数组，不要编造。"
        "characters 必须列出画面里所有的人和动物，哪怕只看到一部分身体；真的没有才返回空数组。"
        "objects 是短名词（不超过 6 字），不要写整句。"
        "只输出 JSON：{\"characters\":[画面里的角色],\"objects\":[可见物品],"
        "\"scene\":\"场景（≤12字）\",\"relations\":[\"角色在做什么（≤15字）\"],\"mood\":\"画面情绪\"}"
    )

    def __init__(self) -> None:
        self.settings = get_settings()

    def analyze_sync(self, image_bytes: bytes, cached: dict | None = None) -> VlmResult:
        if cached and cached.get("status") == "ready":
            return VlmResult(
                characters=cached.get("characters") or [],
                objects=cached.get("objects") or [],
                scene=cached.get("scene") or "",
                relations=cached.get("relations") or [],
                mood=cached.get("mood") or "",
                mode="library_cache",
            )
        if self.settings.vlm_mode != "http":
            return VlmResult([], [], "", [], "", "stub", degraded=True)
        return self._analyze_http(image_bytes)

    def _analyze_http(self, image_bytes: bytes) -> VlmResult:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = json.dumps(
            {
                "model": self.settings.vlm_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.PROMPT},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ],
                    }
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.settings.vlm_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with guard.safe_urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        # Ollama 的视觉模型有时会把 JSON 包在 ``` 里，或被思考过程污染
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = _loose_json(content)
        return VlmResult(
            characters=_short_labels(list(parsed.get("characters") or [])),
            objects=_short_labels(list(parsed.get("objects") or [])),
            scene=str(parsed.get("scene") or ""),
            relations=list(parsed.get("relations") or []),
            mood=str(parsed.get("mood") or ""),
            mode=f"vlm:{self.settings.vlm_model}",
        )