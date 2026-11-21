"""语音测试脚本：模拟 ESP32 客户端，通过 /ws/voice 与百炼 Qwen-Omni-Realtime 对话。

流程：
    1. 读取 test_input.wav，转为 16kHz 16bit mono PCM
    2. 连接 ws://127.0.0.1:8000/ws/voice（服务端纯透传到百炼）
    3. 按百炼 Realtime 协议发送音频（JSON 事件，音频 base64 内嵌，分块发送）
    4. 收集百炼返回的音频流，保存为 test_output.wav（24kHz PCM）

说明：百炼协议为 JSON 文本帧（兼容 OpenAI Realtime），音频不直接以
二进制帧传输，而是 base64 编码后放入 input_audio_buffer.append 事件。
ESP32 设备上只需同样拼装 JSON 字符串即可。
"""

import array
import asyncio
import base64
import json
import wave

from websockets.asyncio.client import connect

VOICE_WS = "ws://127.0.0.1:8000/ws/voice"
INPUT_WAV = "test_input.wav"
OUTPUT_WAV = "test_output.wav"
INPUT_RATE = 16000      # 百炼输入要求：16kHz 16bit mono PCM
OUTPUT_RATE = 24000     # 百炼输出默认：24kHz PCM
CHUNK_BYTES = 3200      # 每块 3200 字节 = 100ms @ 16kHz 16bit
RECV_TIMEOUT = 60       # 单事件接收超时（秒）


def resample(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """线性插值重采样。"""
    if src_rate == dst_rate:
        return samples
    ratio = src_rate / dst_rate
    out = array.array("h")
    for i in range(int(len(samples) / ratio)):
        pos = i * ratio
        i0 = int(pos)
        frac = pos - i0
        s0 = samples[i0]
        s1 = samples[min(i0 + 1, len(samples) - 1)]
        out.append(int(s0 + (s1 - s0) * frac))
    return out


def load_pcm16(wav_path: str, target_rate: int) -> bytes:
    """读取 wav 并输出 target_rate Hz 的 16bit mono PCM。"""
    with wave.open(wav_path, "rb") as w:
        assert w.getsampwidth() == 2, "仅支持 16-bit PCM WAV"
        channels = w.getnchannels()
        src_rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if channels == 2:
        samples = samples[::2]  # 双声道取左声道
    samples = resample(samples, src_rate, target_rate)
    return samples.tobytes()


def save_pcm16_wav(wav_path: str, pcm: bytes, rate: int) -> None:
    """把 16bit mono PCM 保存为 wav 文件。"""
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def main() -> None:
    pcm = load_pcm16(INPUT_WAV, INPUT_RATE)
    duration = len(pcm) / 2 / INPUT_RATE
    print(f"[input] {INPUT_WAV}: {len(pcm)} bytes PCM, {duration:.2f}s @ {INPUT_RATE}Hz")

    output_pcm = bytearray()
    transcript_parts: list[str] = []

    async with connect(VOICE_WS) as ws:
        async def recv_event() -> dict:
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            event = json.loads(raw)
            if event.get("type") == "error":
                raise RuntimeError(f"server error event: {event.get('error')}")
            return event

        async def wait_event(event_type: str) -> dict:
            while True:
                event = await recv_event()
                if event["type"] == event_type:
                    return event

        # 1. 等待会话建立，然后按百炼协议配置会话（Manual 模式，客户端控制回合）
        await wait_event("session.created")
        print("[session] created")

        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "turn_detection": None,  # Manual 模式
                "audio": {
                    "input": {"format": {
                        "type": "pcm",
                        "sample_rate": INPUT_RATE,
                        "sample_format": "s16le",
                        "channels": 1,
                        "packing": "interleaved",
                        "channel_layout": "mono",
                    }},
                    "output": {"format": {"type": "pcm", "sample_rate": OUTPUT_RATE}},
                },
            },
        }))
        await wait_event("session.updated")
        print("[session] updated")

        # 2. 分块发送音频（模拟 ESP32 从麦克风缓冲区循环发送）
        n_chunks = (len(pcm) + CHUNK_BYTES - 1) // CHUNK_BYTES
        for i in range(0, len(pcm), CHUNK_BYTES):
            chunk = pcm[i:i + CHUNK_BYTES]
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            }))
            await asyncio.sleep(0.02)  # 模拟实时节奏，避免瞬间灌入
        print(f"[input] sent {n_chunks} chunks, {len(pcm)} bytes total")

        # 3. 提交缓冲并请求模型回复
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await wait_event("input_audio_buffer.committed")
        print("[input] buffer committed")

        await ws.send(json.dumps({"type": "response.create"}))
        print("[model] waiting for response...")

        # 4. 收集响应：音频 delta 与文本转写
        while True:
            event = await recv_event()
            etype = event["type"]
            if etype == "response.audio.delta":
                output_pcm += base64.b64decode(event["delta"])
            elif etype == "response.audio_transcript.delta":
                transcript_parts.append(event["delta"])
            elif etype == "response.text.delta":
                transcript_parts.append(event["delta"])
            elif etype == "response.done":
                print("[model] response done, status:", event["response"]["status"])
                break
            # 其余事件（如 input_audio_buffer.speech_started 等）忽略

    # 5. 保存结果
    transcript = "".join(transcript_parts)
    if transcript:
        print(f"[model] transcript: {transcript}")
    if output_pcm:
        save_pcm16_wav(OUTPUT_WAV, bytes(output_pcm), OUTPUT_RATE)
        out_duration = len(output_pcm) / 2 / OUTPUT_RATE
        print(f"[output] saved {OUTPUT_WAV}: {len(output_pcm)} bytes, {out_duration:.2f}s @ {OUTPUT_RATE}Hz")
    else:
        print("[output] no audio received")


if __name__ == "__main__":
    asyncio.run(main())
