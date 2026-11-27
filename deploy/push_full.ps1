<#
  push_full.ps1 —— 全量重传的"本机这一半"：打包 + 生成清单 + scp 到服务器

  为什么要有它：服务器上的 full_sync.sh 需要两样东西——一份"新树"和一份"本地清单"。
  这两样都必须在**同一台机器**上、用**同一套规则**生成，否则对账会出现假差异。
  本脚本就是负责生成这两样，并走 frp 传上去。

  用法（三条典型命令）：

    # 后端全量
    pwsh -File push_full.ps1 -Target backend
    # 前端 dist（先在本机 npm run build）
    pwsh -File push_full.ps1 -Target web   -Dist ..\web\front\dist
    # 面板 dist
    pwsh -File push_full.ps1 -Target panel -Dist ..\web\panel\dist

  产物（都在服务器 /tmp 下，同名带时间戳）：
    /tmp/bakerai-full-<ts>/            解包好的新树
    /tmp/bakerai-full-<ts>.tgz         原始包（留档）
    /tmp/manifest-<target>-<ts>.txt    本地清单（<sha256>␠␠<相对路径>）
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('backend', 'web', 'panel')]
    [string]$Target,

    # backend 默认取本仓库的 web/back；web/panel 必须显式给 -Dist
    [string]$Dist,

    [string]$BackendDir = (Resolve-Path (Join-Path $PSScriptRoot '..\web\back')).Path,
    [string]$SshHost = 'baker',          # frp 快通道（124.220.238.163:54528）
    [string]$RemoteTmp = '/tmp'
)

$ErrorActionPreference = 'Stop'
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'

function Push-File($local, $remote) {
    scp -q $local "${SshHost}:${remote}"
    if ($LASTEXITCODE -ne 0) { throw "scp 失败：$local → $remote" }
}

function Make-Manifest($root, $outFile, [string[]]$extraExclude = @()) {
    $args = @((Join-Path $PSScriptRoot 'lib\manifest.py'), 'make', $root, $outFile)
    foreach ($e in $extraExclude) { $args += @('--exclude', $e) }
    python @args
    if ($LASTEXITCODE -ne 0) { throw "生成清单失败：$root" }
}

Write-Host "=== push_full：$Target @ $ts ===" -ForegroundColor Cyan

switch ($Target) {
    'backend' {
        $src = $BackendDir
        $tar = Join-Path $env:TEMP "bakerai-full-$ts.tgz"
        $excl = @('--exclude=.venv', '--exclude=.venv-linux', '--exclude=__pycache__',
                  '--exclude=.mypy_cache', '--exclude=.pytest_cache', '--exclude=.ruff_cache',
                  '--exclude=.env', '--exclude=_runtime', '--exclude=*.egg-info')
        # 只打这五样：app/ schema/ scripts/ requirements.lock pyproject.toml
        tar -czf $tar -C $src @excl app schema scripts requirements.lock pyproject.toml
        if ($LASTEXITCODE -ne 0) { throw "打包失败" }
        $mLocal = Join-Path $env:TEMP "manifest-backend-$ts.txt"
        # 清单只对"这五样"生成：先拼一个临时树，避免把 .venv 之类算进去
        $tree = Join-Path $env:TEMP "bakertree-$ts"
        New-Item -ItemType Directory -Force -Path $tree | Out-Null
        foreach ($d in 'app', 'schema', 'scripts') { Copy-Item -Recurse -Force (Join-Path $src $d) $tree }
        Copy-Item (Join-Path $src 'requirements.lock'), (Join-Path $src 'pyproject.toml') $tree
        Make-Manifest $tree $mLocal
        Remove-Item -Recurse -Force $tree
        Push-File $tar "$RemoteTmp/bakerai-full-$ts.tgz"
        Push-File $mLocal "$RemoteTmp/manifest-backend-$ts.txt"
        Write-Host "  包：$tar（$([math]::Round((Get-Item $tar).Length/1KB)) KB）"
        Write-Host "  清单：$mLocal（$((Get-Content $mLocal).Count) 条）"
        Write-Host ""
        Write-Host "  服务器侧执行：" -ForegroundColor Yellow
        Write-Host "    rm -rf /tmp/bakerai-full-$ts && mkdir -p /tmp/bakerai-full-$ts"
        Write-Host "    tar -xzf /tmp/bakerai-full-$ts.tgz -C /tmp/bakerai-full-$ts"
        Write-Host "    bash /tmp/deploy-new/deploy/full_sync.sh --staged /tmp/bakerai-full-$ts --local-manifest /tmp/manifest-backend-$ts.txt"
    }
    { $_ -in 'web', 'panel' } {
        if (-not $Dist) { throw "-Target $Target 必须给 -Dist <dist 目录>" }
        $distPath = (Resolve-Path $Dist).Path
        if (-not (Test-Path (Join-Path $distPath 'index.html'))) { throw "$distPath 里没有 index.html" }
        $tar = Join-Path $env:TEMP "bakerai-$Target-$ts.tgz"
        tar -czf $tar -C $distPath .
        if ($LASTEXITCODE -ne 0) { throw "打包失败" }
        $mLocal = Join-Path $env:TEMP "manifest-$Target-$ts.txt"
        Make-Manifest $distPath $mLocal --extraExclude '.well-known'
        Push-File $tar "$RemoteTmp/bakerai-$Target-$ts.tgz"
        Push-File $mLocal "$RemoteTmp/manifest-$Target-$ts.txt"
        Write-Host "  dist：$distPath（$([math]::Round((Get-Item $tar).Length/1KB)) KB）"
        Write-Host "  清单：$mLocal（$((Get-Content $mLocal).Count) 条）"
        Write-Host ""
        Write-Host "  服务器侧执行：" -ForegroundColor Yellow
        Write-Host "    rm -rf /tmp/${Target}dist && mkdir -p /tmp/${Target}dist"
        Write-Host "    tar -xzf /tmp/bakerai-$Target-$ts.tgz -C /tmp/${Target}dist"
        Write-Host "    bash /tmp/deploy-new/deploy/full_sync_web.sh --host $(if($Target -eq 'web'){'test2'}else{'panel'}) --dist /tmp/${Target}dist --local-manifest /tmp/manifest-$Target-$ts.txt"
    }
}
