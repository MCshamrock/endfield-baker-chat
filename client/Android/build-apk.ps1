# Android/build-apk.ps1 —— 一条命令：cap copy → gradle → zipalign → apksigner → 校验
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File F:\...\client\Android\build-apk.ps1
#
# 工具链查找顺序（改机器时只需要设环境变量）：
#   $env:BAKER_JDK    JDK 21 根目录
#   $env:BAKER_SDK    Android SDK 根目录
#   $env:BAKER_GRADLE Gradle 8.14.3 的 bin\gradle.bat
# 都没设时用本机缓存 D:\tmp\baker4-build（2026-11-21 打包用的那一套）。
$ErrorActionPreference = 'Stop'

$client = Split-Path -Parent $PSScriptRoot
$android = $PSScriptRoot

$jdk = $env:BAKER_JDK
if (-not $jdk) { $jdk = 'D:\tmp\baker4-build\jdk\jdk-21.0.2' }
$sdk = $env:BAKER_SDK
if (-not $sdk) { $sdk = 'D:\tmp\baker4-build\sdk' }
$gradle = $env:BAKER_GRADLE
if (-not $gradle) { $gradle = 'D:\tmp\baker4-build\gradle\gradle-8.14.3\bin\gradle.bat' }

foreach ($p in @($jdk, $sdk, $gradle)) {
  if (-not (Test-Path $p)) {
    throw "工具链缺失：$p`n请设置 BAKER_JDK / BAKER_SDK / BAKER_GRADLE，或按 README §3.3 准备。"
  }
}

$env:JAVA_HOME = $jdk
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk

Write-Host '== 1/5 同步网页层到 Android ==' -ForegroundColor Cyan
Set-Location $client
npx cap copy android
if ($LASTEXITCODE -ne 0) { throw 'cap copy 失败' }

Write-Host '== 2/5 Gradle assembleRelease ==' -ForegroundColor Cyan
Set-Location $android
& $gradle assembleRelease --no-daemon
if ($LASTEXITCODE -ne 0) { throw 'gradle assembleRelease 失败' }

$unsigned = Join-Path $android 'app\build\outputs\apk\release\app-release-unsigned.apk'
if (-not (Test-Path $unsigned)) { throw "没有找到未签名 APK：$unsigned" }

Write-Host '== 3/5 zipalign ==' -ForegroundColor Cyan
$bt = Join-Path $sdk 'build-tools\36.0.0'
$aligned = Join-Path $env:TEMP 'BakerAI4-aligned.apk'
& (Join-Path $bt 'zipalign.exe') -p -f 4 $unsigned $aligned
if ($LASTEXITCODE -ne 0) { throw 'zipalign 失败' }

Write-Host '== 4/5 apksigner ==' -ForegroundColor Cyan
$props = Get-Content (Join-Path $android 'keys\keystore.properties') -Encoding UTF8
$map = @{}
foreach ($line in $props) {
  if ($line -match '^\s*([^#=]+)\s*=\s*(.*)$') { $map[$matches[1].Trim()] = $matches[2].Trim() }
}
$store = $map['storePassword']
$alias = $map['keyAlias']
if (-not $store -or -not $alias) { throw 'keys\keystore.properties 缺少 storePassword / keyAlias' }

$ver = (Get-Content (Join-Path $client 'package.json') -Raw | ConvertFrom-Json).version
# 产物按版本分目录：Releases\<大>.x.x\<大>.<小>.x\<完整版本>\（与发版目录约定一致）
$parts = $ver.Split('.')
$major = $parts[0]
$minor = $parts[1]
$out = Join-Path $client "Releases\$major.x.x\$major.$minor.x\$ver\BakerAI4-v$ver.apk"
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
& (Join-Path $jdk 'bin\java.exe') -jar (Join-Path $bt 'lib\apksigner.jar') sign `
  --ks (Join-Path $android 'keys\bakerai4.jks') `
  --ks-pass "pass:$store" --key-pass "pass:$store" --ks-key-alias $alias `
  --out $out $aligned
if ($LASTEXITCODE -ne 0) { throw 'apksigner 失败' }

Write-Host '== 5/5 校验 ==' -ForegroundColor Cyan
& (Join-Path $jdk 'bin\java.exe') -jar (Join-Path $bt 'lib\apksigner.jar') verify --print-certs $out
if ($LASTEXITCODE -ne 0) { throw '签名校验失败' }
& (Join-Path $bt 'aapt2.exe') dump badging $out | Select-Object -First 1

$hash = (Get-FileHash $out -Algorithm SHA256).Hash
Write-Host ("完成：" + $out) -ForegroundColor Green
Write-Host ("SHA256: " + $hash)
