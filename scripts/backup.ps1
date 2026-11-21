# 备份 data/ 与 .env 到 backups\，默认保留最近 14 份
#
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Keep 30

param([int]$Keep = 14)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Dir = Join-Path $Root "backups"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$Zip = Join-Path $Dir "reading-ai-$Stamp.zip"

$Items = @()
if (Test-Path (Join-Path $Root "data")) { $Items += (Join-Path $Root "data") }
if (Test-Path (Join-Path $Root ".env")) { $Items += (Join-Path $Root ".env") }
if (-not $Items) { throw "没有可备份的 data/ 或 .env" }

Compress-Archive -Path $Items -DestinationPath $Zip -CompressionLevel Optimal
Write-Host "已备份：$Zip" -ForegroundColor Green

$Old = Get-ChildItem $Dir -Filter "reading-ai-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
foreach ($file in $Old) {
    Remove-Item $file.FullName -Force
    Write-Host "删除旧备份：$($file.Name)"
}
Write-Host ("当前保留 {0} 份" -f (Get-ChildItem $Dir -Filter "reading-ai-*.zip").Count)
