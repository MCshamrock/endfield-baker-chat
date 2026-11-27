# Windows/build.ps1 —— 一条命令：网页层 → Electron 载荷 → 安装器 → 复制到 Releases
#
# 用法（在任意目录）：
#   powershell -ExecutionPolicy Bypass -File F:\...\client\Windows\build.ps1
#
# 前置：Node 20+、Python 3.10+、.NET Framework 的 csc.exe、
#       Electron 43.4.0 的下载缓存（%LOCALAPPDATA%\electron\Cache）。
$ErrorActionPreference = 'Stop'

$client = Split-Path -Parent $PSScriptRoot
Set-Location $client

Write-Host '== 1/3 构建网页层 ==' -ForegroundColor Cyan
npm run build
if ($LASTEXITCODE -ne 0) { throw 'npm run build 失败' }

Write-Host '== 2/3 打 Windows 安装器 ==' -ForegroundColor Cyan
python Windows\installer\tools\build.py
if ($LASTEXITCODE -ne 0) { throw 'build.py 失败' }

$ver = (Get-Content (Join-Path $client 'package.json') -Raw | ConvertFrom-Json).version
$setup = Join-Path $client "Windows\installer\out\BakerAI4-v$ver-Setup.exe"
if (-not (Test-Path $setup)) { throw "没有找到产物：$setup" }

Write-Host '== 3/3 复制到 Releases ==' -ForegroundColor Cyan
$releases = Join-Path $client 'Releases'
New-Item -ItemType Directory -Force -Path $releases | Out-Null
Copy-Item $setup $releases -Force

$hash = (Get-FileHash (Join-Path $releases "BakerAI4-v$ver-Setup.exe") -Algorithm SHA256).Hash
Write-Host ("完成：Releases\BakerAI4-v" + $ver + "-Setup.exe") -ForegroundColor Green
Write-Host ("SHA256: " + $hash)
