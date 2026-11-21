"""语音对话脚本：在电脑上与百炼语音模型完成问答闭环。

流程：
    1. 连接 /ws/voice（模型主动提问时，提问语音会在本机播放）
    2. 模型说完后，可以文字回答，也可以输入 v 用麦克风录音回答
    3. 模型用语音继续对话，循环往复

用法（在 .venv 激活后）：
    python chat_voice.py
    输入文字 = 文字回答；输入 v = 录音 5 秒语音回答；输入 q 退出。
    音频保存在 chat_output.wav。

依赖：sounddevice、numpy（仅语音回答需要，文字回答不需要）。

注意：ESP32 实机上不需要此脚本——孩子对着开发板说话，设备把
麦克风音频按 input_audio_buffer.append 事件发给 /ws/voice 即可
（参考 test_voice.py 的发送方式）。
"""

import asyncio
import base64
import json
import subprocess
import wave
from pathlib import Path

from websockets.asyncio.client import connect

VOICE_WS = "ws://127.0.0.1:8000/ws/voice"
INPUT_RATE = 16000       # 百炼输入采样率
OUTPUT_RATE = 24000      # 百炼输出默认采样率
CHUNK_BYTES = 3200       # 每块 3200 字节 = 100ms @ 16kHz 16bit
RECORD_SECONDS = 5       # 语音回答的录音时长（秒）
OUTPUT_WAV = Path(__file__).resolve().parent / "chat_output.wav"


def play_wav(path: Path) -> None:
    """用 Windows 自带方式播放 wav（子进程执行，不阻塞监听循环）。"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"(New-Object Media.SoundPlayer '{path}').PlaySync()"],
    )


def record_audio(duration: float, samplerate: int) -> bytes:
    """用麦克风录制 duration 秒，返回 16kHz 16bit mono PCM（阻塞）。"""
    import sounddevice as sd
    recording = sd.rec(
        int(duration * samplerate),
        samplerate=samplerate,
        channels=1,
        dtype="int16",
    )
    sd.wait()  # 阻塞到录音完成
    return recording.tobytes()


async def send_text_reply(ws, text: str) -> None:
    """把用户的文字回答作为用户消息发给模型，触发语音回复。"""
    await ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }))
    await ws.send(json.dumps({"type": "response.create"}))


async def send_audio_reply(ws, pcm: bytes) -> None:
    """把录音 PCM 分块发给模型，提交后请求语音回复。"""
    for i in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[i:i + CHUNK_BYTES]
        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode("ascii"),
        }))
    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
    # 等待缓冲提交完成，再请求回复
    while True:
        event = json.loads(await ws.recv())
        if event["type"] == "input_audio_buffer.committed":
            break
    await ws.send(json.dumps({"type": "response.create"}))


async def receive_and_play(ws) -> tuple[str, bool]:
    """收集一轮模型回复（音频+转写），保存并播放，返回 (转写文本, 是否有音频)。"""
    audio = bytearray()
    transcript: list[str] = []
    while True:
        event = json.loads(await ws.recv())
        etype = event["type"]
        if etype == "response.audio.delta":
            audio += base64.b64decode(event["delta"])
        elif etype in ("response.audio_transcript.delta", "response.text.delta"):
            transcript.append(event["delta"])
        elif etype == "response.done":
            text = "".join(transcript)
            if audio:
                with wave.open(str(OUTPUT_WAV), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(OUTPUT_RATE)
                    w.writeframes(bytes(audio))
                play_wav(OUTPUT_WAV)
            return text, bool(audio)
        # 其余事件（如 response.audio.done 等）忽略


async def main() -> None:
    print(f"连接语音服务 {VOICE_WS} ...")
    async with connect(VOICE_WS) as ws:
        # 等待会话建立，并配置音频格式（16kHz 输入 / 24kHz 输出）
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if event["type"] == "session.created":
                break
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "turn_detection": None,
                "audio": {
                    "input": {"format": {"type": "pcm", "sample_rate": 16000}},
                    "output": {"format": {"type": "pcm", "sample_rate": OUTPUT_RATE}},
                },
            },
        }))
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if event["type"] == "session.updated":
                break
        print("就绪！上传绘本图片（POST /analyze-page）后，模型的提问会在这里播放；")
        print("模型说完后：直接输入文字回答，或输入 v 录音回答（5 秒），q 退出。\n")

        while True:
            # 1. 等待模型开口（提问或回复），播放并打印转写
            text, has_audio = await receive_and_play(ws)
            if text:
                print(f"\n[模型] {text}")
            print("=" * 40)

            # 2. 读用户回答：文字直接输入；v 用麦克风录音；q 退出
            try:
                answer = (await asyncio.to_thread(
                    input, "[你] 输入文字回答（v 录音回答 / q 退出）: ")).strip()
            except EOFError:
                print("\nstdin 已关闭，退出。")
                break
            if not answer:
                continue
            if answer.lower() == "q":
                print("再见！")
                break
            if answer.lower() == "v":
                print(f"开始录音（{RECORD_SECONDS} 秒），请对着麦克风说话...")
                pcm = await asyncio.to_thread(record_audio, RECORD_SECONDS, INPUT_RATE)
                print(f"录音完成（{len(pcm)} 字节），正在发送...")
                await send_audio_reply(ws, pcm)
            else:
                await send_text_reply(ws, answer)


if __name__ == "__main__":
    asyncio.run(main())
