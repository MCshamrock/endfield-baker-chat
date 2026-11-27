# check.ps1 —— 一次性跑完全部检查：格式/风格 → 类型 → 测试
#
# 为什么一个脚本跑三样而不是分开跑：
#   三样里任何一样挂了，这刀就算没做完。分开跑会出现"本地 ruff 过了"、
#   "本地测试过了"这种各说各话。
#
# 用法：
#   pwsh -File web\back\scripts\check.ps1
#   pwsh -File web\back\scripts\check.ps1 -SkipTests   # 只跑静态检查，快速自查

param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

$Back      = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Root      = (Resolve-Path (Join-Path $Back '..\..')).Path
$Venv      = Join-Path $Back '.venv'
$Runtime   = Join-Path $Root '_runtime'

if (-not (Test-Path "$Venv\Scripts\python.exe")) {
    throw "还没有 .venv。先跑：pwsh -File scripts/setup_venv.ps1"
}

# 缓存也留在 BakerAI_new 里（ruff 默认写当前目录的 .ruff_cache，mypy 写 .mypy_cache，都在 back 下，没问题）
$env:TMP  = Join-Path $Runtime 'tmp'
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null

Push-Location $Back
try {
    Write-Host '=== 1/3 ruff（风格 / 未用变量 / 复杂度）===' -ForegroundColor Cyan
    & "$Venv\Scripts\ruff.exe" check app tests scripts
    if ($LASTEXITCODE -ne 0) { throw "ruff 没过" }

    Write-Host '=== 2/3 mypy（类型）===' -ForegroundColor Cyan
    & "$Venv\Scripts\mypy.exe"
    if ($LASTEXITCODE -ne 0) { throw "mypy 没过" }

    if ($SkipTests) {
        Write-Host '（跳过测试）' -ForegroundColor Yellow
    } else {
        Write-Host '=== 3/3 pytest ===' -ForegroundColor Cyan
        & "$Venv\Scripts\pytest.exe"
        if ($LASTEXITCODE -ne 0) { throw "测试没过" }
    }

    Write-Host ''
    Write-Host '全部通过。' -ForegroundColor Green
}
finally {
    Pop-Location
}
