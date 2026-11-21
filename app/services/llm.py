"""园内 OpenAI 兼容推理客户端（vLLM / Ollama / llama.cpp 均可）。

只通过 guard.safe_urlopen 出站：地址一旦写到园外，请求在发出前就被拦下。
"""

from __future__ import annotations

import json
import re
import urllib.request

from .. import guard
from ..config import get_settings


class LLMError(RuntimeError):
    pass


def _extract_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"模型没有返回合法 JSON：{text[:120]}")


class LocalLLM:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.llm_mode == "http"

    def chat_json(self, system: str, user: str, temperature: float = 0.2):
        if not self.enabled:
            raise LLMError("未启用本地 LLM（LLM_MODE=stub）")
        payload = json.dumps(
            {
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.settings.llm_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with guard.safe_urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # 网络/服务异常统一转成 LLMError，由 Brain 降级
            raise LLMError(str(exc)) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError("模型响应结构异常") from exc
        usage = data.get("usage") or {}
        meta = {
            "tokens_in": int(usage.get("prompt_tokens") or 0),
            "tokens_out": int(usage.get("completion_tokens") or 0),
            "model": data.get("model") or self.settings.llm_model,
        }
        return _extract_json(content), meta