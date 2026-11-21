# 接入园内本地模型（把 stub 换成 http）

系统默认 `*_MODE=stub`，用确定性规则跑通全部流程，**不访问外网**。
真实部署时把 `.env` 里的模式改成 `http`，指向园内 GPU 机器即可，上层编排、A11 自检、
界面都不需要改一行代码。

## 1. 五个服务与接口契约

| 环节 | 变量 | 请求 | 期望响应 |
| --- | --- | --- | --- |
| A1 文字识别 | `OCR_MODE/OCR_URL` | `POST {OCR_URL}/ocr`，JSON `{"image_b64": "...", "lang": "ch"}` | `{"text": "整页文字", "lines": [{"text": "..."}], "confidence": 0.96}` |
| A2 画面理解 | `VLM_MODE/VLM_URL/VLM_MODEL` | `POST {VLM_URL}/chat/completions`，OpenAI 兼容、含 `image_url`、`response_format=json_object` | `choices[0].message.content` 是一段 JSON：`{"characters":[],"objects":[],"scene":"","relations":[],"mood":""}` |
| A3/A4/A7/A10 文本 | `LLM_MODE/LLM_URL/LLM_MODEL` | `POST {LLM_URL}/chat/completions`，OpenAI 兼容 | 同上，content 为 JSON |
| A6 童声识别 | `ASR_MODE/ASR_URL` | `POST {ASR_URL}/transcribe`，`{"audio_b64":"...","language":"zh","sample_rate":16000}` | `{"text": "...", "confidence": 0.87}` |
| A5 语音合成 | `TTS_MODE/TTS_URL/TTS_VOICE` | `POST {TTS_URL}/synthesize`，`{"text":"...","voice":"...","format":"wav"}` | 直接返回 wav 二进制 |

所有出站请求都经过 `app/guard.py`：
只允许 `localhost`、`10/8`、`172.16/12`、`192.168/16`、`*.local` 以及你显式写进
`ALLOWED_HOSTS` 的域名。写到公网地址会在**发请求之前**抛 `OfflineViolation`，
不存在"改个开关就放行"的路径。

## 2. 一个可直接照抄的启动方式

GPU 机器上（示例，显卡 24G，四个服务分开进程）：

```bash
# A2 画面理解：Qwen3-VL 8B
vllm serve Qwen/Qwen3-VL-8B-Instruct --port 9102 --max-model-len 8192

# A3/A4/A7/A10 文本：Qwen3 8B
vllm serve Qwen/Qwen3-8B-Instruct --port 9103 --max-model-len 8192
```

OCR 服务自己包一层（PaddleOCR 官方没有 HTTP 接口，20 行 FastAPI 即可）：

```python
# ocr_server.py
import base64, io
from fastapi import FastAPI
from paddleocr import PaddleOCR
from PIL import Image

app = FastAPI()
ocr = PaddleOCR(use_angle_cls=True, lang="ch")

@app.post("/ocr")
def read(payload: dict):
    image = Image.open(io.BytesIO(base64.b64decode(payload["image_b64"])))
    result = ocr.ocr(image, cls=True) or []
    lines = [{"text": item[1][0], "confidence": item[1][1]} for page in result for item in page]
    return {
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "confidence": round(sum(line["confidence"] for line in lines) / max(len(lines), 1), 3),
    }
```

ASR / TTS 同理：FunASR 的 `AutoModel(model="paraformer-zh")` 接 `/transcribe`，
CosyVoice / 园区已有 TTS 接 `/synthesize` 返回 wav。

## 3. 切换步骤

1. 复制 `.env.example` 为 `.env`，填园内地址，例如：
   ```ini
   OCR_MODE=http
   VLM_MODE=http
   LLM_MODE=http
   ASR_MODE=http
   TTS_MODE=http
   OCR_URL=http://192.168.1.21:9101
   VLM_URL=http://192.168.1.21:9102/v1
   LLM_URL=http://192.168.1.21:9103/v1
   ASR_URL=http://192.168.1.21:9104
   TTS_URL=http://192.168.1.21:9105
   ```
2. 重启 `python run.py`，打开教师端 →「自检与运维」→「运行自检」：
   - `OCR/VLM/LLM/ASR/TTS 服务` 应变成 **ok**（带 `host:port 可达`）；
   - 仍是 warn 说明还在 stub，或端口连不上；
   - `无园外请求配置` 必须保持 ok。
3. 打开「内容自检演示」跑一遍，确认闸门仍在拦（自检是规则，不依赖模型）。

## 4. 第一次跑真实模型要校的三件事

- **三层摘要的接地**：A3 只允许重组 A1/A2 的事实。若模型爱写"它心里很难过"，
  自检会判 `desc_speculation`，让它改一次；仍改不掉就降级为规则版摘要。
  教师端每条记录都能看到 `自检通过 / 已修正 / 已拦截·降级`。
- **提问长度**：4-6 岁 20 字、3-4 岁 12 字、3 岁以下 8 字（英文按词：10 词内）。
  超长会被判 fail 并重写，别把阈值调大来"通过"，那是把问题推给孩子。
- **童声 ASR 置信度**：默认 `ASR_CONFIRM_THRESHOLD=0.75`，低于它就走
  "你是不是说 XX？"确认式追问。园内实测童声普遍偏低，可以用真实录音跑
  `python _tools\live_smoke.py` 观察确认率，再决定 0.7~0.85 之间取值。

## 5. 性能与容量参考

| 指标 | 目标（规范 §4.3） | 说明 |
| --- | --- | --- |
| 拍照 → 开始提问 | ≤ 12 s | A1‖A2 并行；库内页面命中缓存时通常 < 1 s |
| 单页模型调用 | OCR 1 次 + VLM 1 次 + LLM 2~4 次 | 自检"重写一次"会多 1 次 |
| 显存 | VLM 8B + LLM 8B ≈ 2×16G（或一张 48G 卡量化共跑） | 8G 卡建议只接 OCR+ASR，VLM/LLM 用更小模型 |

单页链路有埋点（`app/services/metrics.py`），教师端「自检与运维」里能看到
今日调用数、平均耗时与最慢页面 Top 10，可直接对照验收指标。