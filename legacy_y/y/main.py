"""FastAPI WebSocket 服务。

启动方式（在 .venv 激活后）：
    uvicorn main:app --reload
WebSocket 端点：
    ws://127.0.0.1:8000/ws/echo  回显测试
    ws://127.0.0.1:8000/ws/voice 百炼 Qwen-Omni-Realtime 实时语音透传
    （语音模型地址由 .env 的 DASHSCOPE_REALTIME_URL 决定，当前使用 qwen3.8-omni-flash-realtime）
REST 端点：
    POST /analyze-page  上传 JPEG，百炼多模态模型分析（qwen3.8-omni-flash）
"""

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from websockets.asyncio.client import connect

# 加载 .env 文件（如果存在），可通过 os.getenv() 读取其中的变量
load_dotenv()

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent

# 百炼实时语音默认地址（旧域名，无需业务空间 ID）
# 新模型（如 qwen3.8-omni-flash-realtime）须使用业务空间专属域名，
# 在 .env 中设置 DASHSCOPE_REALTIME_URL 覆盖即可。
DASHSCOPE_REALTIME_DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={model}"
DASHSCOPE_REALTIME_DEFAULT_MODEL = "qwen3.5-omni-plus-realtime"

# 百炼多模态视觉模型（/analyze-page 使用）
DASHSCOPE_VISION_DEFAULT_MODEL = "qwen3.8-omni-flash"

# 当前活跃的语音会话（/ws/voice 建立百炼连接后注册，供 /analyze-page 推送提问）
# 单设备场景：后连接的设备会覆盖前一个，断开时若自己仍是被注册的那个才清除。
# busy 表示百炼当前是否正在输出回复（response.created 与 response.done 之间为 True），
# 由透传循环在转发事件时顺带更新，供翻页打断逻辑使用。
_voice_session: dict | None = None

# 当前排队的延迟提问任务（翻页后留阅读时间再提问；连续翻页时取消旧任务）
_voice_question_task: asyncio.Task | None = None


def build_picture_question(analysis: dict) -> str:
    """根据绘本图片识别结果，构造给语音模型的幼儿提问指令。"""
    characters = "、".join(analysis.get("characters") or []) or "未识别到人物"
    return (
        "下面是绘本新翻到的一页画面（之前页面的问答已经结束，请忘记旧话题）的识别结果：\n"
        f"画面文字：{analysis.get('page_text') or '无'}\n"
        f"画面人物：{characters}\n"
        f"画面场景：{analysis.get('scene_description') or '无'}\n\n"
        "请你用温柔、活泼的语气，向 3-6 岁的小朋友提一个与画面内容相关的互动小问题，"
        "引导孩子观察画面或猜想故事情节。只提一个问题，句子简短，"
        "不要任何解释、开场白或称呼。"
        "如果孩子回答'不知道'，请温柔地直接说出答案并简单解释；"
        "如果孩子反问'为什么'，请先认真回答他的问题，再决定是否追问。"
    )


async def ask_voice_model_after_delay(prompt: str, delay: float) -> bool:
    """留出阅读时间后下发提问：延迟 delay 秒再调用 ask_voice_model。

    连续翻页时取消上一个还未执行的延迟任务，只保留最新一页的提问。

    返回是否已成功安排（会话不存在时为 False；任务被取消返回 False）。
    """
    global _voice_question_task
    if _voice_question_task is not None and not _voice_question_task.done():
        _voice_question_task.cancel()
    if _voice_session is None:
        return False

    async def delayed_ask() -> bool:
        try:
            await asyncio.sleep(max(delay, 0))
        except asyncio.CancelledError:
            return False
        return await ask_voice_model(prompt)

    _voice_question_task = asyncio.get_running_loop().create_task(delayed_ask())
    return True


async def ask_voice_model(prompt: str) -> bool:
    """通过已建立的百炼语音连接，让语音模型开口说话。

    翻页即结束：若模型上一轮回复还在进行中（busy），先发 response.cancel
    打断，等回合结束（response.done）后再推送新问题，避免回合冲突。

    返回是否成功下发（当前没有活跃语音连接时为 False）。
    """
    session = _voice_session
    if session is None:
        return False
    upstream = session["upstream"]
    loop = asyncio.get_running_loop()
    try:
        # 模型还在说话：取消当前回复，并等待回合结束
        if session.get("busy"):
            await upstream.send(json.dumps({"type": "response.cancel"}))
            deadline = loop.time() + 5
            while session.get("busy") and loop.time() < deadline:
                await asyncio.sleep(0.05)
        await upstream.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        }))
        await upstream.send(json.dumps({"type": "response.create"}))
        return True
    except Exception:
        return False


def dashscope_http_base_url() -> str:
    """构造百炼 OpenAI 兼容模式 HTTP 地址。

    优先使用 .env 的 DASHSCOPE_HTTP_BASE_URL；否则从 DASHSCOPE_REALTIME_URL
    提取业务空间域名；都没有时回退到旧域名。
    """
    configured = os.getenv("DASHSCOPE_HTTP_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    realtime = os.getenv("DASHSCOPE_REALTIME_URL", "").strip()
    if realtime:
        return f"https://{urlsplit(realtime).netloc}/compatible-mode/v1"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def describe_image_with_dashscope(image_bytes: bytes, api_key: str, model: str) -> dict:
    """调用百炼多模态模型分析图片，返回结构化字段。"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "你是一个图像内容分析助手。请分析这张图片，只返回一个 JSON 对象，"
        "不要输出任何其他文字。JSON 格式："
        "{\"page_text\": \"图片中出现的所有文字内容，按顺序拼接为一段字符串，没有文字则为空字符串\", "
        "\"characters\": [\"图片中出现的人物或角色，字符串数组，没有则为空数组\"], "
        "\"scene_description\": \"对图片场景的一句话中文描述\"}"
    )
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "response_format": {"type": "json_object"},
    })
    request = urllib.request.Request(
        f"{dashscope_http_base_url()}/chat/completions",
        data=payload.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    try:
        result = json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to parse model output: {exc}") from exc
    return {
        "page_text": result.get("page_text", ""),
        "characters": result.get("characters", []),
        "scene_description": result.get("scene_description", ""),
    }


@app.get("/")
async def root() -> FileResponse:
    """返回浏览器可用的 WebSocket 测试页面。"""
    return FileResponse(BASE_DIR / "index.html")


@app.post("/analyze-page")
def analyze_page(file: UploadFile = File(...)) -> dict:
    """接收 JPEG 图片上传，调用百炼多模态模型分析，返回结构化 JSON。

    返回字段：page_text（图片中的文字）、characters（人物/角色数组）、
    scene_description（场景描述）。
    """
    image_bytes = file.file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty file")
    if not image_bytes.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=400, detail="only JPEG images are supported")

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="DASHSCOPE_API_KEY not set in .env")
    model = os.getenv("DASHSCOPE_VISION_MODEL", DASHSCOPE_VISION_DEFAULT_MODEL).strip()

    try:
        result = describe_image_with_dashscope(image_bytes, api_key, model)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=502, detail=f"dashscope HTTP {exc.code}: {body}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"dashscope call failed: {exc}") from exc

    # 若语音会话在线，先留出阅读时间（QUESTION_DELAY_SECONDS，默认 8 秒），
    # 到点后再让语音模型向幼儿提问
    voice_triggered = False
    delay = 8.0
    session = _voice_session
    if session is not None:
        try:
            delay = float(os.getenv("QUESTION_DELAY_SECONDS", "8").strip() or 8)
        except ValueError:
            delay = 8.0
        future = asyncio.run_coroutine_threadsafe(
            ask_voice_model_after_delay(build_picture_question(result), delay),
            session["loop"],
        )
        try:
            voice_triggered = bool(future.result(timeout=5))
        except Exception:
            voice_triggered = False
    result["voice_triggered"] = voice_triggered
    result["question_delay_seconds"] = delay if voice_triggered else 0
    return result


@app.websocket("/ws/echo")
async def websocket_echo(websocket: WebSocket) -> None:
    """收到什么文本就原样返回什么。"""
    await websocket.accept()
    while True:
        try:
            data = await websocket.receive_text()
        except WebSocketDisconnect:
            break
        await websocket.send_text(data)


@app.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket) -> None:
    """接入百炼 Qwen-Omni-Realtime 实时语音：浏览器 <-> 百炼 双向透传。

    浏览器发来的文本/二进制帧原样转发给百炼，
    百炼返回的文本/二进制帧原样发回浏览器，不做任何解析或转换。
    """
    await websocket.accept()

    global _voice_session  # 注册/清理当前语音会话，供 /analyze-page 推送提问

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        await websocket.send_text(json.dumps(
            {"type": "error", "message": "DASHSCOPE_API_KEY not set in .env"}))
        await websocket.close()
        return

    upstream_url = (
        os.getenv("DASHSCOPE_REALTIME_URL", "").strip()
        or DASHSCOPE_REALTIME_DEFAULT_URL.format(
            model=os.getenv("DASHSCOPE_VOICE_MODEL", DASHSCOPE_REALTIME_DEFAULT_MODEL).strip()
        )
    )

    try:
        async with connect(
            upstream_url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
        ) as upstream:
            # 注册当前语音会话，供 /analyze-page 推送提问指令
            session = {"upstream": upstream, "loop": asyncio.get_running_loop(), "busy": False}
            _voice_session = session
            try:
                async def browser_to_upstream() -> None:
                    """浏览器 -> 百炼：原样转发文本或二进制帧。"""
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                break
                            if message.get("text") is not None:
                                await upstream.send(message["text"])
                            elif message.get("bytes") is not None:
                                await upstream.send(message["bytes"])
                    except WebSocketDisconnect:
                        pass
                    finally:
                        # 浏览器断开后，关闭上游让另一个方向的转发也结束
                        await upstream.close()

                async def upstream_to_browser() -> None:
                    """百炼 -> 浏览器：原样转发文本或二进制帧，同时跟踪回合状态。"""
                    async for message in upstream:
                        if isinstance(message, str):
                            # 顺带跟踪百炼回合状态（不修改内容，仍原样转发）
                            try:
                                etype = json.loads(message).get("type")
                                if etype == "response.created":
                                    session["busy"] = True
                                elif etype == "response.done":
                                    session["busy"] = False
                            except (json.JSONDecodeError, AttributeError):
                                pass
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
                    # 百炼断开后，关闭浏览器连接让另一个方向的转发也结束
                    await websocket.close()

                await asyncio.gather(
                    browser_to_upstream(),
                    upstream_to_browser(),
                    return_exceptions=True,
                )
            finally:
                # 连接结束，若自己仍是被注册的会话则清除
                if _voice_session is session:
                    _voice_session = None
    except Exception as exc:
        # 连接百炼失败（如 401 鉴权失败），尽力把错误告知浏览器
        try:
            await websocket.send_text(json.dumps(
                {"type": "error", "message": f"dashscope connect failed: {exc}"}))
        except Exception:
            pass
        await websocket.close()
