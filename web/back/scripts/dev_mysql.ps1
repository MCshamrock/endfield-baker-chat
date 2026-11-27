# dev_mysql.ps1 —— 本地开发用 MySQL（给 BakerAI 新后端）
#
# 三条硬规矩：
#   1. **所有文件都在 BakerAI_new 里面**：程序、数据、配置、日志、下载的 zip，
#      一概落在 <BakerAI_new>/_runtime 下。不写 C:\Temp、不写 %LOCALAPPDATA%、不写 WSL 的 /home。
#   2. 端口 3307，不碰 3306（那是 CloverWebChat 的库，动了会连累另一个项目）。
#   3. 独立实例：用 --defaults-file 指向本目录的 my.ini，不受系统已装的 MySQL 影响。
#
# 为什么不用 WSL 里那套 MySQL（实测结论，别再来一遍）：
#   数据目录放在 /mnt/d（Windows 盘，走 9p）时，mysqld 建 Unix socket 会报
#   "Bind on unix socket: Operation not supported"，而 socket 必须放在 Linux 原生盘 —— 那就必然越界。
#   所以改用 Windows 便携版：没有 socket 问题，I/O 也快，还不会因为 WSL 休眠而停。
#
# 用法（在任意目录都能跑）：
#   pwsh -File scripts/dev_mysql.ps1 install   # 解压 + 初始化（只需第一次）
#   pwsh -File scripts/dev_mysql.ps1 start
#   pwsh -File scripts/dev_mysql.ps1 stop
#   pwsh -File scripts/dev_mysql.ps1 status
#   pwsh -File scripts/dev_mysql.ps1 reset     # 删库重来（会先停服务）
#   pwsh -File scripts/dev_mysql.ps1 createdb  # 建 baker_back 库 + baker 账号

param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'start', 'stop', 'status', 'reset', 'createdb')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

# ---- 路径：本脚本在 <BakerAI_new>/web/back/scripts/ 下，往上三层就是 BakerAI_new
$Root    = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Base    = Join-Path $Root '_runtime\mysql'          # 这一个目录装下程序 / 数据 / 配置 / 日志
$Program = Join-Path $Base  'env'                    # 解压出来的 mysql-8.x.x-winx64
$DataDir = Join-Path $Base  'data'
$Ini     = Join-Path $Base  'my.ini'
$ErrLog  = Join-Path $Base  'err.log'
$ZipDir  = Join-Path $Root  '_runtime\downloads'
$Port    = 3307

function Get-MySqlHome {
    # 解压后的目录名带版本号（mysql-8.4.13-winx64）。这里按名字倒序取最新那个：
    # "mysql-8.4.13" > "mysql-8.0.46"（字典序对这几段数字恰好成立）。
    # 之所以要"取最新"，是因为本机要保持和服务器同版本（服务器 8.4.x），
    # 旧版本目录留着不影响，但不能被误选。
    if (-not (Test-Path $Program)) { return $null }
    $dir = Get-ChildItem $Program -Directory -Filter 'mysql-*' | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $dir) { return $null }
    return $dir.FullName
}

function Get-MySqlExe([string]$name) {
    # 注意：变量别叫 $home —— 那是 PowerShell 的只读自动变量（用户主目录），赋值会直接报错
    $mysqlRoot = Get-MySqlHome
    if (-not $mysqlRoot) { throw "还没安装，先跑：pwsh -File scripts/dev_mysql.ps1 install" }
    $exe = Join-Path $mysqlRoot "bin\$name"
    if (-not (Test-Path $exe)) { throw "找不到 $exe" }
    return $exe
}

function Test-Alive {
    # 注意：不能用 `mysqladmin ping` 判断存活 —— 它对"服务器在、但密码不对"也返回 0，
    # 会误以为自己的实例已经起来了（实测踩过：连到别的实例上，报 1698 却说 alive）。
    # 正确做法：真的跑一句 SELECT 1，只有认证通过才算活着。
    try {
        $mysql = Get-MySqlExe 'mysql.exe'
        $null = & $mysql "--defaults-file=$Ini" '--user=root' '--host=127.0.0.1' '--connect-timeout=2' '-N' '-e' 'SELECT 1' 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Write-Ini {
    # my.ini 里路径要用正斜杠，反斜杠会被 MySQL 当成转义
    $d = $DataDir.Replace('\', '/')
    $l = $ErrLog.Replace('\', '/')
    @"
[mysqld]
port=$Port
bind-address=127.0.0.1
datadir=$d
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci
# 默认时区设成 UTC：项目所有时间都存 UTC，避免"服务器在 +08:00"这种隐式转换
default-time-zone='+00:00'
log-error=$l
max_allowed_packet=64M
# X Protocol 用不上，关掉少开一个端口
mysqlx=0
# 别加 skip-name-resolve：加了之后 127.0.0.1 不会被反解成 localhost，
# 初始化时建的 root@localhost 就匹配不上，连 TCP 会报 1130 "Host not allowed"（实测踩过）

# 客户端段必须有：不加的话 mysql.exe 会以为端口是默认的 3306，
# 直接连到 CloverWebChat 那个库上去（实测踩过，报 1698 密码错，很容易看错方向）
[client]
port=$Port
host=127.0.0.1
default-character-set=utf8mb4
"@ | Set-Content -Path $Ini -Encoding ASCII
}

function Do-Install {
    New-Item -ItemType Directory -Force -Path $Base, $Program, $ZipDir | Out-Null

    if (Get-MySqlHome) {
        Write-Host "程序已解压：$(Get-MySqlHome)"
    } else {
        $zip = Get-ChildItem $ZipDir -Filter 'mysql-*-winx64.zip' | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $zip) { throw "没找到安装包。先下载到 $ZipDir（见 docs/00-设计定稿-v1.md §8）" }
        Write-Host "解压 $($zip.Name) …（约 1 分钟）"
        # 优先用 Windows 自带的 tar.exe 解 zip（比 Expand-Archive 快好几倍，这包有上万个文件）；
        # 万一系统上没有 tar，再退回 Expand-Archive
        $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
        if ($tar) {
            & $tar.Source -xf $zip.FullName -C $Program
            if ($LASTEXITCODE -ne 0) { throw "tar 解压失败（exit $LASTEXITCODE）" }
        } else {
            Expand-Archive -Path $zip.FullName -DestinationPath $Program -Force
        }
        Write-Host "解压完成：$(Get-MySqlHome)"
    }

    Write-Ini

    if (Test-Path (Join-Path $DataDir 'mysql')) {
        Write-Host "数据目录已存在，跳过初始化：$DataDir"
    } else {
        Write-Host "初始化数据目录（root 空密码，只监听 127.0.0.1）…"
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        $mysqld = Get-MySqlExe 'mysqld.exe'
        $p = Start-Process -FilePath $mysqld `
            -ArgumentList @("--defaults-file=$Ini", '--initialize-insecure', '--console') `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput (Join-Path $Base 'init.out') `
            -RedirectStandardError  (Join-Path $Base 'init.err')
        if ($p.ExitCode -ne 0) {
            Write-Host "初始化失败，看 $Base\init.err 最后 20 行："
            Get-Content (Join-Path $Base 'init.err') -Tail 20
            throw "install 失败"
        }
        Write-Host "初始化完成"
    }
}

function Do-Start {
    if (-not (Test-Path $Ini)) { throw "还没 install，先跑 install" }
    if (Test-Alive) { Write-Host "已经在跑了（127.0.0.1:$Port）"; return }

    $mysqld = Get-MySqlExe 'mysqld.exe'
    # 后台起：交给独立进程，pwsh 退出后它继续跑
    # 后台起：交给独立进程，pwsh 退出后它继续跑。
    # 用 -WindowStyle Hidden 给它一个**独立控制台**，别让子进程继承本脚本的 stdout 管道——
    # 继承了的话，调用方（终端 / 自动化工具）会一直等管道关闭，表现为"命令卡住不返回"（实测踩过）。
    # mysqld 自己的日志走 my.ini 里的 log-error，这里不需要再重定向。
    Start-Process -FilePath $mysqld -ArgumentList @("--defaults-file=$Ini") -WindowStyle Hidden | Out-Null

    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Alive) { Write-Host "OK —— MySQL 起来了（127.0.0.1:$Port）"; Do-Status; return }
    }
    Write-Host "起不来。$ErrLog 最后 20 行："
    if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 20 }
    throw "start 失败"
}

function Do-Stop {
    if (-not (Test-Alive)) {
        # 可能"进程在、但认证不通"（例如 root 账号被改坏）。这种情况下 mysqladmin 也关不掉，
        # 直接按进程名杀，别让人卡在"停不下来"上。
        $leftover = Get-Process mysqld -ErrorAction SilentlyContinue
        if ($leftover) {
            Write-Host "认证不通，直接结束进程（$($leftover.Count) 个）"
            $leftover | Stop-Process -Force
        } else {
            Write-Host "本来就没在跑"
        }
        return
    }
    $admin = Get-MySqlExe 'mysqladmin.exe'
    Start-Process -FilePath $admin -ArgumentList @("--defaults-file=$Ini", '--user=root', '--host=127.0.0.1', 'shutdown') -NoNewWindow -Wait | Out-Null
    Write-Host "已停"
}

function Do-Status {
    if (-not (Test-Alive)) { Write-Host "状态：stopped"; return }
    $mysql = Get-MySqlExe 'mysql.exe'
    $sql = "SELECT CONCAT('version   = ', VERSION());" +
           "SELECT CONCAT('charset   = ', @@character_set_server);" +
           "SELECT CONCAT('collation = ', @@collation_server);" +
           "SELECT CONCAT('datadir   = ', @@datadir);" +
           "SELECT CONCAT('time_zone = ', @@time_zone);"
    Write-Host "状态：running (127.0.0.1:$Port)"
    & $mysql "--defaults-file=$Ini" --user=root --host=127.0.0.1 -N -e $sql
}

function Do-CreateDb {
    if (-not (Test-Alive)) { throw "MySQL 没跑，先 start" }
    $mysql = Get-MySqlExe 'mysql.exe'
    $sql = @"
CREATE DATABASE IF NOT EXISTS baker_back CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS baker_back_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'baker'@'%' IDENTIFIED BY 'baker_local_2026';
GRANT ALL PRIVILEGES ON baker_back.* TO 'baker'@'%';
GRANT ALL PRIVILEGES ON baker_back_test.* TO 'baker'@'%';
FLUSH PRIVILEGES;
"@
    $sql | & $mysql "--defaults-file=$Ini" --user=root --host=127.0.0.1
    if ($LASTEXITCODE -ne 0) { throw "建库失败（exit $LASTEXITCODE），看上面的报错" }
    Write-Host "已建库 baker_back（开发）/ baker_back_test（测试）+ 账号 baker"
}

function Do-Reset {
    Do-Stop
    if (Test-Path $DataDir) {
        Remove-Item -Recurse -Force $DataDir
        Write-Host "已删除数据目录 $DataDir"
    }
    Do-Install
    Do-Start
    Do-CreateDb
}

switch ($Action) {
    'install'  { Do-Install }
    'start'    { Do-Start }
    'stop'     { Do-Stop }
    'status'   { Do-Status }
    'createdb' { Do-CreateDb }
    'reset'    { Do-Reset }
}
