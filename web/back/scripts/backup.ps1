# backup.ps1 —— 给任务计划程序用的薄包装
#
# 为什么要有它：任务计划程序直接调 python 时，工作目录不是项目目录，
# `.env` 读不到、`_runtime` 路径也会算错（settings 里的相对路径按 cwd 解析）。
# 这里只做一件事：切到 web/back 再调 backup.py，别的一概不管。
#
# 手动跑：
#   pwsh -File scripts/backup.ps1              # 备份 + 自检
#   pwsh -File scripts/backup.ps1 -Verify      # 再多做一次真恢复校验

param(
    [switch]$Verify,
    [int]$Keep = 14
)

$ErrorActionPreference = 'Stop'

$BackDir = Split-Path -Parent $PSScriptRoot
Set-Location $BackDir

$Python = Join-Path $BackDir '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw "找不到 $Python —— 先跑 scripts/setup_venv.ps1"
}

$args = @('scripts/backup.py', '--keep', "$Keep")
if ($Verify) { $args += '--verify' }

& $Python @args
exit $LASTEXITCODE
