"""语音提问监听端：让电脑扮演设备端，播放百炼语音模型的提问。

用法：
    1. 运行本脚本（窗口保持开启）
    2. 另开终端/网页上传图片（POST /analyze-page 或 http://127.0.0.1:8000/docs）
    3. 模型生成的提问语音会在本机播放，音频同时保存为 question_output.wav

原理：/analyze-page 识别图片后，通过 /ws/voice 这条连接让语音模型
主动开口，音频流经透传推给本脚本，脚本解码、保存并播放。
"""

import asyncio
import base64
import json
import subprocess
import wave
from pathlib import Path

from websockets.asyncio.client import connect

VOICE_WS = "ws://127.0.0.1:8000/ws/voice"
OUTPUT_RATE = 24000      # 百炼输出默认采样率
OUTPUT_WAV = Path(__file__).resolve().parent / "question_output.wav"


def play_wav(path: Path) -> None:
    """用 Windows 自带方式播放 wav（子进程执行，不阻塞监听循环）。"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"(New-Object Media.SoundPlayer '{path}').PlaySync()"],
    )


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
        print("就绪！保持本窗口开启。")
        print("上传图片（POST /analyze-page）后，模型的提问语音会在这里播放。Ctrl+C 退出。\n")

        # 持续监听：收集每一轮回复的音频与转写，回合结束后保存并播放
        audio = bytearray()
        transcript: list[str] = []
        collecting = False
        while True:
            event = json.loads(await ws.recv())
            etype = event["type"]
            if etype == "response.audio.delta":
                collecting = True
                audio += base64.b64decode(event["delta"])
            elif etype in ("response.audio_transcript.delta", "response.text.delta"):
                transcript.append(event["delta"])
            elif etype == "response.done":
                if collecting and audio:
                    question = "".join(transcript)
                    print(f"\n[提问] {question}")
                    with wave.open(str(OUTPUT_WAV), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(OUTPUT_RATE)
                        w.writeframes(bytes(audio))
                    print(f"[音频] 已保存 {OUTPUT_WAV}，正在播放...")
                    play_wav(OUTPUT_WAV)
                audio = bytearray()
                transcript = []
                collecting = False


if __name__ == "__main__":
    asyncio.run(main())
