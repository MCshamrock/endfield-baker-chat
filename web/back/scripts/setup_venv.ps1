# setup_venv.ps1 —— 建虚拟环境 + 按锁文件装依赖
#
# 三条规矩：
#   1. **Python 版本跟服务器一致**：3.14（服务器 3.14.4，本机 3.14.5）
#   2. **依赖版本跟服务器一致**：装 requirements.lock，不按 pyproject 的范围自己解析
#   3. **别把文件写到 BakerAI_new 外面**：pip 缓存、临时文件全指到 _runtime
#
# 用法：
#   pwsh -File web\back\scripts\setup_venv.ps1
#   pwsh -File web\back\scripts\setup_venv.ps1 -Recreate   # 环境坏了/版本变了就重建

param(
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'

$Back    = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path      # web\back
$Root    = (Resolve-Path (Join-Path $Back '..\..')).Path           # BakerAI_new
$Venv    = Join-Path $Back '.venv'
$Runtime = Join-Path $Root '_runtime'
$Lock    = Join-Path $Back 'requirements.lock'
$WantPy  = '3.14'

# ---- pip 缓存 / 临时文件都搬进 BakerAI_new
$env:PIP_CACHE_DIR = Join-Path $Runtime 'cache\pip'
$env:TMP           = Join-Path $Runtime 'tmp'
$env:TEMP          = $env:TMP
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR, $env:TMP | Out-Null

function Get-PyVersion([string]$exe) {
    try { return (& $exe -c "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null) }
    catch { return $null }
}

function Find-Python([string]$want) {
    # 候选顺序：常见安装路径 → PATH 里的 python
    $candidates = @(
        "C:\Python$($want.Replace('.',''))\python.exe",
        "C:\Users\ASUS\AppData\Roaming\uv\python\cpython-$want.*-windows-x86_64-none\python.exe"
    )
    foreach ($pattern in $candidates) {
        $hit = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit -and (Get-PyVersion $hit.FullName) -eq $want) { return $hit.FullName }
    }
    foreach ($name in @('python', 'python3')) {
        $exe = (Get-Command $name -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
        if ($exe -and (Get-PyVersion $exe) -eq $want) { return $exe }
    }
    throw "找不到 Python $want。服务器用的是 $want，本机也必须是它，别用别的版本装依赖。"
}

# ---- 已有 .venv 但版本不对 → 自动重建（避免"忘了删，装到旧解释器上"）
if (Test-Path "$Venv\Scripts\python.exe") {
    $have = Get-PyVersion "$Venv\Scripts\python.exe"
    if ($have -ne $WantPy) {
        Write-Host "现有 .venv 是 Python $have，需要 $WantPy → 重建"
        $Recreate = $true
    }
}

if ($Recreate -and (Test-Path $Venv)) {
    Write-Host "删除旧的 .venv …"
    Remove-Item -Recurse -Force $Venv
}

if (-not (Test-Path "$Venv\Scripts\python.exe")) {
    $python = Find-Python $WantPy
    Write-Host "解释器：$python"
    Write-Host "创建 .venv …"
    & $python -m venv $Venv
}

$venvPython = Join-Path $Venv 'Scripts\python.exe'
Write-Host "venv Python：$(& $venvPython -V 2>&1)"

# ---- 先装锁文件里的精确版本，再用 --no-deps 把本项目装成可编辑包
# --no-deps 很关键：否则 pip 会按 pyproject 的范围重新解析，把锁好的版本顶掉
Write-Host "按 requirements.lock 安装（首次约 1 分钟）…"
& $venvPython -m pip install --disable-pip-version-check -q -r $Lock
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败（exit $LASTEXITCODE）" }

& $venvPython -m pip install --disable-pip-version-check -q --no-deps -e $Back
if ($LASTEXITCODE -ne 0) { throw "本项目安装失败（exit $LASTEXITCODE）" }

Write-Host ""
Write-Host "完成。虚拟环境：$Venv"
& $venvPython -c "import fastapi, sqlalchemy, pymysql, pydantic, sys; print('python', sys.version.split()[0]); print('fastapi', fastapi.__version__); print('sqlalchemy', sqlalchemy.__version__); print('pydantic', pydantic.VERSION)"
