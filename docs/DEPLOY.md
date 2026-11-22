# 部署手册（园内一台 Windows 电脑即可跑全链路）

本系统不依赖云端：文字识别、语音合成、童声识别在本机进程内跑；
提问/理解/建议与看图由本机 Ollama 提供。装好之后**运行时不需要联网**。

## 0. 准备

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Windows 10/11 64 位 | 语音用系统自带 SAPI 中文女声 |
| Python | 3.10 及以上 | 安装时勾选 “Add python.exe to PATH” |
| Ollama | 最新版 | <https://ollama.com/download>，装完保持默认端口 11434 |
| ffmpeg | 任意 6.x/7.x | `winget install Gyan.FFmpeg`，装完重开终端 |
| 硬件 | 8GB 显存 + 16GB 内存起步 | 3B 语言/视觉模型可跑；纯 CPU 也能跑，只是慢 |
| 磁盘 | 预留 10GB | 模型约 5.6GB + 语音识别模型 480MB |

摄像头：电脑 USB 摄像头即可，系统里能看到的第 1 个摄像头就是 `camera:0`。

## 1. 一键安装

在仓库根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本会依次做（重复运行安全）：

1. 检查 Python / Ollama / ffmpeg；
2. 建 `.venv` 并安装依赖（`rapidocr-onnxruntime`、`faster-whisper`、`pyttsx3`、`pywin32`、`opencv-python` 等）；
3. 下载真实模型：`models/whisper`（faster-whisper small）+ Ollama 的 `qwen2.5:3b`、`qwen2.5vl:3b`；
4. 生成 `.env`（随机 `SECRET_KEY`、6 位教师口令、8 位管理员口令），并打印口令；
5. 放行 TCP 8000、把启动脚本放进“启动”目录（开机自启）。

> 防火墙规则需要管理员权限：用“管理员 PowerShell”运行才能自动放行；
> 普通权限下脚本会跳过这一步并提示，事后手动加也很快（见下文局域网排查）。

常用参数：

```powershell
# 内网 pip 镜像 + 只装环境不下载模型
scripts\install.ps1 -PipIndex https://pypi.tuna.tsinghua.edu.cn/simple -NoModels

# 只给本机用（不监听局域网、不加防火墙规则、不自启）
scripts\install.ps1 -NoLan -NoAutoStart
```

装完核对模型：

```powershell
.venv\Scripts\python.exe scripts\fetch_models.py --check
```

## 2. 启动与自检

```powershell
scripts\run_server.bat
```

打开 <http://127.0.0.1:8000/device-check>：先看浏览器能力、摄像头、麦克风、喇叭、网络；
再在「共读引擎（服务端）」块点「真跑五个引擎」（需先在教师端登录），应看到五项全 ✅：

- OCR 识字：认出「绘本共读」
- VLM 看画面：看到画面里的物体
- LLM 生成：回答「好」
- TTS 出声：sapi5，生成了音频
- ASR 转写：把刚合成的音频转回文字，相似度高

两项补强：教师端 →「运维」→「运行自检」看依赖/端口/离线/绘本库；
「深度自检」则是真调模型跑完上面五条链路（约 20 秒，每项带实测耗时）。

只要有一项 `warn/stub`，首页会显示“演示模式”，说明该环节还没接上真实引擎。

## 3. 局域网访问（平板/另一台电脑）

`.env` 里 `HOST=0.0.0.0` 即为局域网模式。在别的设备上打开：

```
http://<这台电脑的局域网 IP>:8000
```

查看 IP：`ipconfig`（形如 `192.168.1.23`）。打不开时检查：

1. Windows 防火墙是否放行了 TCP 8000（`scripts\install.ps1` 会自动加；也可手动加）；
2. 两台设备是否在同一网段（同一路由器）；
3. 电脑是否睡眠/休眠（电源选项里设为“从不”）。

> 出站方向仍然只允许本机与园内私有网段（`app/guard.py` 强制），即使 `.env` 写错，
> 请求也会在发出前被拦下，不存在把数据送出园外的路径。

## 4. 摄像头怎么接

三种方式，都在教师端「会话」页的“摄像头”面板里：

| 方式 | 填写 | 说明 |
| --- | --- | --- |
| 本机 USB 摄像头 | `camera:0` | 服务端直接拍帧（推荐，最稳） |
| 网络摄像头 | `http://192.168.1.50/capture` | 支持带 Basic 认证；只允许私有地址 |
| RTSP | `rtsp://user:pass@192.168.1.51/stream` | 经 `_tools/camera_bridge.py` 转 HTTP（需 opencv） |

`camera:1`、`camera:2` 对应第 2、3 个摄像头。占用中的摄像头会报
“摄像头打开失败”，关掉占用它的软件（微信、相机等）再试。

## 5. 备份与恢复

```powershell
# 备份 data/ 和 .env 到 backups\reading-ai-时间戳.zip，保留最近 14 份
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1

# 自定义保留份数
scripts\backup.ps1 -Keep 30

# 直接装成每天 20:00 自动备份（安装时就定好）
scripts\install.ps1 -BackupAt 20:00
```

恢复：停服务 → 解压覆盖 `data/` 与 `.env` → 启动。

## 6. 卸载自启 / 换端口

- 取消自启：删除“启动”目录里的 `绘本阅读AI.lnk`
  （`shell:startup` 可打开该目录）。
- 换端口：改 `.env` 的 `PORT`，同时把防火墙规则里的 8000 改掉。
- 换模型：改 `.env` 的 `LLM_MODEL` / `VLM_MODEL`（Ollama 里的名字），重启服务。
- 护眼提醒：`EYE_REST_MINUTES` 默认 20 分钟，设为 0 关闭；改完重启服务。
  机器人端只在“连续用眼”满点时提一次，上一页提问会先让位给休息提醒。
- 阅读档案：教师端「阅读档案」选孩子就能看页数/绘本/时长/回应/提问，数据全从
  `turns/sessions/free_qas` 直查聚合；接口是 `GET /api/teacher/reading/{孩子id}?days=14`，
  复核用 `python _tools\recheck.py profile`（会拿接口数字回 SQLite 对账）。
- 关掉某个引擎：把对应 `*_MODE` 改为 `stub` 会退回确定性规则（首页会提示演示模式），
  一般不建议在验收/生产环境这么做。

## 7. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 自检里 OCR 报“本机 OCR 没装” | `.venv` 里缺 `rapidocr-onnxruntime`，重跑 `install.ps1` |
| ASR 报“音频转码失败” | 没装 ffmpeg 或不在 PATH，装完重开终端 |
| ASR 报“本机 ASR 没装/模型缺失” | `scripts\fetch_models.py --only asr` |
| LLM 慢（>15s） | 首次加载模型会慢；持续慢就看显存是否被占满，或换更小模型 |
| 提问变成英文 | 已内置中文校验：模型输出非中文会自动回退到规则文案，并在教师端标注 |
| 摄像头打不开 | 被其它软件占用，或 USB 没插好；先关掉相机/微信再试 |
| 说不清的错误 | 日志在 `data/server.log`（5MB 自动轮转，留 3 份），把末尾 50 行发给开发即可定位 |
| 网页打不开 | 服务是否在跑（窗口是否关掉）、端口是否被占（`netstat -ano | findstr 8000`） |
