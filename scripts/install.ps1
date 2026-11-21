# 一键部署：把本仓库在这台 Windows 电脑上装成可用的园内系统
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# 做的事（重复运行安全）：
#   1. 检查 Python 3.10+ / Ollama / ffmpeg
#   2. 建 .venv 并装依赖（首次联网）
#   3. 下载真实模型（RapidOCR 随依赖安装；Whisper / Qwen 走 fetch_models.py）
#   4. 生成 .env（随机 SECRET_KEY 与口令，真实引擎模式）
#   5. 打开局域网端口 + 开机自启（可用参数关闭）

param(
    [switch]$NoModels,      # 只装环境，不下载模型
    [switch]$NoLan,         # 不监听局域网、不加防火墙规则
    [switch]$NoAutoStart,   # 不设开机自启
    [string]$PipIndex = "", # 例如 https://pypi.tuna.tsinghua.edu.cn/simple
    [string]$BackupAt = ""  # 例如 20:00，每天定时备份一次
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "== 绘本阅读陪伴 AI：一键部署 ==" -ForegroundColor Cyan
Write-Host "仓库目录：$Root"

# ---------- 1. Python ----------
$Python = ""
$PythonPre = @()
foreach ($pair in @(@("py", "-3"), @("python", ""))) {
    $exe = $pair[0]
    $pre = @($pair[1] | Where-Object { $_ })
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $ver = & $exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -and [version]$ver -ge [version]"3.10") {
            $Python = $exe
            $PythonPre = $pre
            break
        }
    } catch { }
}
if (-not $Python) { throw "没有找到 Python 3.10+，请先安装 https://www.python.org/downloads/windows/ 并勾选 Add to PATH" }
$PythonLabel = (@($Python) + $PythonPre) -join " "
Write-Host "[1/5] Python OK（$PythonLabel，$ver）" -ForegroundColor Green

# ---------- 2. 虚拟环境与依赖 ----------
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[2/5] 创建虚拟环境 .venv ..."
    & $Python @PythonPre -m venv "$Venv"
}
if (-not (Test-Path $VenvPython)) { throw "虚拟环境创建失败：$Venv" }
$PipArgs = @("-m", "pip", "install", "--upgrade", "pip", "-q")
if ($PipIndex) { $PipArgs += @("-i", $PipIndex) }
& $VenvPython @PipArgs
$ReqArgs = @("-m", "pip", "install", "-r", "requirements.txt")
if ($PipIndex) { $ReqArgs += @("-i", $PipIndex) }
& $VenvPython @ReqArgs
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，检查网络或加 -PipIndex 指向园内镜像" }
Write-Host "[2/5] 依赖安装完成" -ForegroundColor Green

# ---------- 3. ffmpeg / Ollama / 模型 ----------
$Ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $Ffmpeg) {
    Write-Warning "没有 ffmpeg：语音识别需要它。装法见 docs\DEPLOY.md（winget install Gyan.FFmpeg）"
} else {
    Write-Host "[3/5] ffmpeg OK" -ForegroundColor Green
}
if (-not $NoModels) {
    & $VenvPython "scripts\fetch_models.py"
    if ($LASTEXITCODE -ne 0) { throw "模型下载失败，可重跑：.venv\Scripts\python.exe scripts\fetch_models.py" }
} else {
    Write-Host "[3/5] 跳过模型下载（-NoModels）"
}

# ---------- 4. .env ----------
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Write-Host "[4/5] .env 已存在，保留不动（要重建请先删除）" -ForegroundColor Yellow
} else {
    $Secret = & $VenvPython -c "import secrets;print(secrets.token_hex(32))"
    $TeacherPin = & $VenvPython -c "import secrets;print(''.join(secrets.choice('0123456789') for _ in range(6)))"
    $AdminPin = & $VenvPython -c "import secrets;print(''.join(secrets.choice('0123456789') for _ in range(8)))"
    $Host_ = if ($NoLan) { "127.0.0.1" } else { "0.0.0.0" }
    $Lines = @(
        "# 由 scripts/install.ps1 生成；真实本机引擎，运行时不联网",
        "HOST=$Host_",
        "PORT=8000",
        "SECRET_KEY=$Secret",
        "TEACHER_PIN=$TeacherPin",
        "ADMIN_PIN=$AdminPin",
        "OFFLINE_ONLY=1",
        "DATA_DIR=data",
        "",
        "# 五个引擎：ocr/asr/tts 在本进程内跑；llm/vlm 走本机 Ollama",
        "OCR_MODE=local",
        "ASR_MODE=local",
        "TTS_MODE=local",
        "LLM_MODE=http",
        "VLM_MODE=http",
        "LLM_URL=http://127.0.0.1:11434/v1",
        "LLM_MODEL=qwen2.5:3b",
        "LLM_MAX_TOKENS=768",
        "VLM_URL=http://127.0.0.1:11434/v1",
        "VLM_MODEL=qwen2.5vl:3b",
        "ASR_MODEL=small",
        "ASR_DEVICE=cpu",
        "ASR_COMPUTE_TYPE=int8",
        "ASR_MODEL_DIR=models/whisper",
        "TTS_LOCAL_VOICE=Microsoft Huihui Desktop",
        "FFMPEG_BIN=ffmpeg",
        "",
        "QUESTION_DELAY_SECONDS=8",
        "LISTEN_TIMEOUT_SECONDS=20",
        "TEACHER_TIMEOUT_SECONDS=15",
        "PAGE_LIMIT_PER_SESSION=60",
        "DAILY_CALL_LIMIT=3000",
        "ASR_CONFIRM_THRESHOLD=0.75",
        "KEEP_PAGE_IMAGE=0"
    )
    Set-Content -Path $EnvFile -Value $Lines -Encoding UTF8
    Write-Host "[4/5] 已生成 .env" -ForegroundColor Green
    Write-Host ("      教师口令 {0} / 管理员口令 {1}（请抄到纸上，勿外传）" -f $TeacherPin, $AdminPin) -ForegroundColor Yellow
}

# ---------- 5. 局域网 + 开机自启 ----------
if (-not $NoLan) {
    $rule = "绘本阅读AI-8000"
    if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
        try {
            New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow | Out-Null
            Write-Host "[5/5] 防火墙已放行 8000 端口" -ForegroundColor Green
        } catch {
            Write-Warning "防火墙规则没加上（需要管理员）。请用管理员 PowerShell 重跑本脚本，或手动放行 TCP 8000"
        }
    }
}
if (-not $NoAutoStart) {
    $Startup = [Environment]::GetFolderPath("Startup")
    $Shortcut = Join-Path $Startup "绘本阅读AI.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut($Shortcut)
    $Link.TargetPath = Join-Path $Root "scripts\run_server.bat"
    $Link.WorkingDirectory = $Root
    $Link.WindowStyle = 7
    $Link.Description = "绘本阅读陪伴 AI 服务"
    $Link.Save()
    Write-Host "[5/5] 已设开机自启（$Shortcut）" -ForegroundColor Green
}
if ($BackupAt) {
    $TaskName = "绘本阅读AI-每日备份"
    $BackupCmd = "powershell -ExecutionPolicy Bypass -File `"$Root\scripts\backup.ps1`""
    schtasks /Create /TN $TaskName /TR $BackupCmd /SC DAILY /ST $BackupAt /F | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[5/5] 已设定每日 $BackupAt 自动备份（任务：$TaskName）" -ForegroundColor Green
    } else {
        Write-Warning "定时备份没加上，可手动：schtasks /Create /TN $TaskName /TR `"$BackupCmd`" /SC DAILY /ST $BackupAt"
    }
}

Write-Host ""
Write-Host "部署完成。启动：scripts\run_server.bat" -ForegroundColor Cyan
Write-Host "自检：  浏览器打开 http://127.0.0.1:8000/device_check" -ForegroundColor Cyan
