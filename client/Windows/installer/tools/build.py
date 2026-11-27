# build.py —— 打包全流程：载荷 -> 7z -> 安装器外壳 -> 拼装
#
# 用法（在项目根跑）：
#   python setup\tools\build.py                # 全量（含重新压载荷，慢）
#   python setup\tools\build.py --skip-payload # 只重编外壳并拼装（快）
#   python setup\tools\build.py --skip-payload --skip-shell
#
# 产物：setup\out\BakerAI-3.1-安装程序.exe
# 载荷：setup\payload\  （app\ + 使用说明.txt + Saira-OFL.txt + 卸载 Baker AI 4.exe）
#
# 为什么用 Python 而不是 ps1：脚本里的中文文件名在 PowerShell 5.1 默认按 ANSI 读会乱码。

import argparse
import glob
import os
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))          # setup\tools
SETUP = os.path.dirname(HERE)                             # Windows\installer
ROOT = os.path.dirname(os.path.dirname(SETUP))            # client（工程根）

SRC = os.path.join(SETUP, 'src')
OUT = os.path.join(SETUP, 'out')
PAYLOAD = os.path.join(SETUP, 'payload')
APP = os.path.join(PAYLOAD, 'app')
RES_DIR = os.path.join(APP, 'resources')
ASAR_OUT = os.path.join(RES_DIR, 'app.asar')
ASAR_STAGE = os.path.join(PAYLOAD, '_appstage')

DIST = os.path.join(ROOT, 'dist')
APP_SRC = os.path.join(ROOT, 'Windows', 'app')             # main.cjs / preload.cjs / paths.cjs / package.json


def find_electron_zip():
    """定位 Electron 运行时 zip（electron 的下载缓存）。

    顺序：BAKER_ELECTRON_ZIP 环境变量 → %LOCALAPPDATA%\\electron\\Cache 里**任意版本**的
    electron-v*-win32-x64.zip（取最新） → 仓库内 electron\\ 下的同名 zip。
    以前这里写死了某个版本的缓存哈希，electron 一升级路径就变、打包直接失败。
    """
    env_zip = os.environ.get('BAKER_ELECTRON_ZIP')
    if env_zip and os.path.exists(env_zip):
        return env_zip
    patterns = [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'electron', 'Cache', '*', 'electron-v*-win32-x64.zip'),
        os.path.join(ROOT, 'electron', 'electron-v*-win32-x64.zip'),
        os.path.join(ROOT, 'node_modules', 'electron', 'dist', 'electron-v*-win32-x64.zip'),
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat))
    if not found:
        return ''
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found[0]


ELECTRON_ZIP = find_electron_zip()

NODE = shutil.which('node') or 'node'
ASAR_JS = os.path.join(ROOT, 'node_modules', '@electron', 'asar', 'bin', 'asar.js')
CSC = r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
SEVEN_Z = os.path.join(ROOT, 'node_modules', 'electron-winstaller', 'vendor', '7z-x64.exe')
RCEDIT = os.path.join(ROOT, 'node_modules', 'electron-winstaller', 'vendor', 'rcedit.exe')
TAR = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'tar.exe')

import json as _json
VERSION = _json.load(open(os.path.join(ROOT, 'package.json'), encoding='utf-8'))['version']
# 版本资源要 4 段；3 段版本号补 .0（3.2.1 -> 3.2.1.0）
FILEVER = VERSION + '.0' if VERSION.count('.') == 2 else VERSION
PRODUCT = 'Baker AI 4'
APP_EXE_NAME = 'Baker AI 4.exe'                    # 进程名/任务管理器名称就是它（去掉扩展名）
APP_DESC = '明日方舟：终末地 Baker AI 4 聊天桌面客户端'
UNINSTALL_NAME = 'Uninstall Baker AI 4.exe'
SETUP_EXE = os.path.join(OUT, 'BakerAI4-v%s-Setup.exe' % VERSION)

CS_SHELL = ['Ef.cs', 'SetupForm.cs', 'PromptForm.cs', 'Installer.cs', 'Notes.cs', 'Facts.cs']
CS_UNINSTALL = ['Ef.cs', 'PromptForm.cs', 'Uninstall.cs', 'Facts.cs']  # 版本号取 Facts.cs


def run(args, cwd=None, capture=False):
    print('  $ ' + ' '.join(args if isinstance(args, list) else [args]))
    kwargs = {'cwd': cwd}
    if capture:
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.STDOUT
        r = subprocess.run(args, **kwargs)
        out = r.stdout.decode('utf-8', 'replace') if r.stdout else ''
        if r.returncode != 0:
            print(out)
            raise SystemExit('命令失败: %s' % args)
        return out
    r = subprocess.run(args, **kwargs)
    if r.returncode != 0:
        raise SystemExit('命令失败: %s' % args)
    return ''


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%.1f %s' % (n, unit)) if unit != 'B' else ('%d B' % n)
        n /= 1024.0


def tree_stats(path):
    files = 0
    total = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return files, total


def make_icon_and_fonts():
    need = ['BakerAI.ico', 'cover.png', 'BakerSairaField.ttf', 'BakerSairaLabel.ttf',
            'BakerSairaDisplay.ttf', 'Saira-OFL.txt']
    missing = [n for n in need if not os.path.exists(os.path.join(OUT, n))]
    if not missing:
        return
    print('== 生成图标与字体 ==')
    run([sys.executable, os.path.join(HERE, 'make_assets.py')])


def extract_electron():
    """electron zip -> payload\\app；electron.exe 改名 Baker AI 4.exe"""
    target = os.path.join(APP, APP_EXE_NAME)
    if os.path.exists(target):
        print('== 载荷 app\\ 已存在，沿用 ==')
        return
    if not os.path.exists(ELECTRON_ZIP):
        raise SystemExit('找不到 Electron 运行时: %s' % ELECTRON_ZIP)
    print('== 解出 Electron 运行时 ==')
    if os.path.exists(APP):
        shutil.rmtree(APP)
    os.makedirs(APP)
    import zipfile
    with zipfile.ZipFile(ELECTRON_ZIP) as z:
        z.extractall(APP)
    for old in ('electron.exe', 'BakerAI.exe'):
        p = os.path.join(APP, old)
        if os.path.exists(p):
            os.replace(p, target)
    # 有 resources\app 时 default_app.asar 不再需要
    old_asar = os.path.join(APP, 'resources', 'default_app.asar')
    if os.path.exists(old_asar):
        os.remove(old_asar)


def set_version_language(exe, old_lang=0x0409, new_lang=0x0804):
    """把版本资源的语言改成中文(中国)：StringTable 键 + VarFileInfo/Translation 一起改。

    old_lang=0x0409 → rcedit 写的英文资源（键 040904b0）
    old_lang=0      → csc 写的中性资源（键 000004b0）
    """
    data = bytearray(open(exe, 'rb').read())
    code = 0x04B0
    old_key = ('%04x%04x' % (old_lang, code)).encode('utf-16-le')
    new_key = ('%04x%04x' % (new_lang, code)).encode('utf-16-le')
    n_key = data.count(old_key)
    if n_key:
        data = data.replace(old_key, new_key)

    # Translation 只改紧跟在 "Translation" 后面的那一处，别误伤别处的同类字节
    # （大 exe 里可能有同名的调试字符串，要逐个找、认准后面跟着旧语言码的那个）
    marker = 'Translation'.encode('utf-16-le')
    old_tr = bytes([old_lang & 0xFF, (old_lang >> 8) & 0xFF, code & 0xFF, (code >> 8) & 0xFF])
    new_tr = bytes([new_lang & 0xFF, (new_lang >> 8) & 0xFF, code & 0xFF, (code >> 8) & 0xFF])
    n_tr = 0
    start = 0
    while True:
        pos = data.find(marker, start)
        if pos < 0:
            break
        seg = data[pos:pos + 96]
        k = seg.find(old_tr)
        if k >= 0:
            at = pos + k
            data[at:at + 4] = new_tr
            n_tr = 1
            break
        start = pos + 1

    if not n_key and not n_tr:
        # 已经是中文(中国)了（rcedit 这次沿用了旧语言表）——不算失败
        if data.count(new_key):
            print('  version language: already zh-CN')
            return True
        return False
    open(exe, 'wb').write(data)
    print('  version language -> zh-CN (key %d, translation %d)' % (n_key, n_tr))
    return True


def ensure_branding():
    """给 Baker AI 4.exe 换图标与版本信息（rcedit），再把版本资源语言改成中文(中国)。
    任务栏、快捷方式、属性页、任务管理器都跟着变。"""
    exe = os.path.join(APP, APP_EXE_NAME)
    icon = os.path.join(OUT, 'BakerAI.ico')
    marker = os.path.join(OUT, '.brand-stamp')
    stamp = '%d|%d|%s|%s|%s|%s' % (os.path.getsize(icon), int(os.path.getmtime(icon)),
                                   APP_EXE_NAME, APP_DESC, VERSION, PRODUCT)
    if os.path.exists(marker):
        with open(marker, 'r', encoding='utf-8') as f:
            if f.read().strip() == stamp:
                return
    print('== 写 exe 图标与版本信息 ==')
    run([RCEDIT, exe, '--set-icon', icon,
         '--set-file-version', FILEVER,
         '--set-product-version', FILEVER,
         '--set-version-string', 'ProductName', PRODUCT,
         '--set-version-string', 'FileDescription', APP_DESC,
         '--set-version-string', 'Comments', APP_DESC,
         '--set-version-string', 'CompanyName', PRODUCT,
         '--set-version-string', 'LegalCopyright', '第三方同人作品，角色版权归鹰角网络'])
    # rcedit 沿用原资源的语言（英文），这里改成中文(中国)
    if not set_version_language(exe, 0x0409):
        print('  !! 语言没改成')
    with open(marker, 'w', encoding='utf-8') as f:
        f.write(stamp)


def write_app_resources():
    """resources\\app.asar：主进程三件套 + package.json + icon + dist

    为什么打成 asar 而不是 resources\\app 目录：Electron 43 在这台 Windows
    （11 Canary 26300）上，用**未打包目录**启动时会在 ready 阶段触发
    CHECK 断点（0x80000003），打成一个 app.asar 就正常。asar 还顺带减少了
    安装包体积、避免用户误改程序文件。
    """
    print('== 写 resources\\app.asar ==')
    if not os.path.exists(os.path.join(DIST, 'index.html')):
        raise SystemExit('dist\\index.html 不存在，先跑 npm run build')
    if os.path.exists(ASAR_STAGE):
        shutil.rmtree(ASAR_STAGE)
    os.makedirs(ASAR_STAGE)
    for name in ('main.cjs', 'preload.cjs', 'paths.cjs', 'package.json'):
        shutil.copy2(os.path.join(APP_SRC, name), os.path.join(ASAR_STAGE, name))
    shutil.copy2(os.path.join(OUT, 'BakerAI.ico'), os.path.join(ASAR_STAGE, 'icon.ico'))
    shutil.copytree(DIST, os.path.join(ASAR_STAGE, 'dist'))

    os.makedirs(RES_DIR, exist_ok=True)
    if os.path.exists(ASAR_OUT):
        os.remove(ASAR_OUT)
    if not os.path.exists(ASAR_JS):
        raise SystemExit('缺少 @electron/asar：%s' % ASAR_JS)
    run([NODE, ASAR_JS, 'pack', ASAR_STAGE, ASAR_OUT], capture=True)
    shutil.rmtree(ASAR_STAGE, ignore_errors=True)

    # 旧的未打包目录留着会让 Electron 优先读目录、绕过 asar
    legacy = os.path.join(RES_DIR, 'app')
    if os.path.exists(legacy):
        shutil.rmtree(legacy)
    print('  app.asar %s' % human(os.path.getsize(ASAR_OUT)))


def write_payload_root():
    print('== 写载荷根文件 ==')
    uninst = os.path.join(OUT, 'uninstall.exe')
    if not os.path.exists(uninst):
        raise SystemExit('缺 uninstall.exe，先编译卸载程序')
    shutil.copy2(os.path.join(SRC, '使用说明.txt'), os.path.join(PAYLOAD, '使用说明.txt'))
    shutil.copy2(os.path.join(OUT, 'Saira-OFL.txt'), os.path.join(PAYLOAD, 'Saira-OFL.txt'))
    shutil.copy2(uninst, os.path.join(PAYLOAD, UNINSTALL_NAME))
    # 旧名字的残留清掉，避免 7z 里同时出现两个卸载器
    for stale in ('卸载 Baker AI 4.exe',):
        p = os.path.join(PAYLOAD, stale)
        if os.path.exists(p):
            os.remove(p)


def compile_cs(out_name, sources, extra=()):
    args = [CSC, '/nologo', '/target:winexe', '/optimize+', '/platform:anycpu', '/codepage:65001',
            '/win32icon:' + os.path.join(OUT, 'BakerAI.ico'),
            '/win32manifest:' + os.path.join(SRC, 'app.manifest'),
            '/resource:%s,BakerSairaField.ttf' % os.path.join(OUT, 'BakerSairaField.ttf'),
            '/resource:%s,BakerSairaLabel.ttf' % os.path.join(OUT, 'BakerSairaLabel.ttf'),
            '/resource:%s,BakerSairaDisplay.ttf' % os.path.join(OUT, 'BakerSairaDisplay.ttf'),
            '/out:' + out_name,
            '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.dll', '/r:System.Core.dll']
    for e in extra:
        args.append(e)
    args += [os.path.join(SRC, s) for s in sources]
    return run(args, capture=True)


def build_uninstaller():
    print('== 编译卸载程序 ==')
    exe = os.path.join(OUT, 'uninstall.exe')
    compile_cs(exe, CS_UNINSTALL)
    set_version_language(exe, 0)          # csc 写的是「语言中性」资源
    print('  uninstall.exe %s' % human(os.path.getsize(exe)))


def compress_payload():
    print('== 压载荷（7z / lzma2 -mx=9）==')
    arc = os.path.join(OUT, 'payload.7z')
    if os.path.exists(arc):
        os.remove(arc)
    run([SEVEN_Z, 'a', '-t7z', '-m0=lzma2', '-mx=9', '-mmt=on', '-y', arc,
         'app', '使用说明.txt', 'Saira-OFL.txt', UNINSTALL_NAME], cwd=PAYLOAD)
    print('  payload.7z %s' % human(os.path.getsize(arc)))


def count_entries(arc):
    out = run([TAR, '-tf', arc], capture=True)
    n = len([ln for ln in out.splitlines() if ln.strip()])
    return n


def write_facts():
    print('== 写 Facts.cs（真实数字）==')
    app_files, app_bytes = tree_stats(APP)
    payload_files, payload_bytes = tree_stats(PAYLOAD)
    entries = count_entries(os.path.join(OUT, 'payload.7z'))
    helper_files = 3   # 卸载程序 + 使用说明 + 字体许可
    text = '''// Facts.cs —— 打包时由 setup\\tools\\build.py 生成（改这里会被覆盖）
namespace BakerSetup
{
    internal static class Facts
    {
        public const string Version = "%s";
        public const string BuildDate = "%s";
        public const int AppFiles = %d;
        public const int HelperFiles = %d;
        public const int DataDirs = 5;
        public const int Entries = %d;
        public const string AppSizeText = "%s";
        public const string InstalledSizeText = "%s";
        public const string PayloadSizeText = "%s";
    }
}
''' % (VERSION, datetime.now().strftime('%Y.%m.%d'), app_files, helper_files, entries,
       human(app_bytes), human(payload_bytes), human(os.path.getsize(os.path.join(OUT, 'payload.7z'))))
    with open(os.path.join(SRC, 'Facts.cs'), 'w', encoding='utf-8') as f:
        f.write(text)
    print('  app %d 文件 / %s；载荷 %d 文件 / %s；tar 条目 %d'
          % (app_files, human(app_bytes), payload_files, human(payload_bytes), entries))


def build_shell():
    print('== 编译安装器外壳 ==')
    exe = os.path.join(OUT, 'setup-shell.exe')
    # 内嵌 7zr.exe 做兜底解压：老系统没有 tar.exe / PATH 里是 GNU tar 时仍能装
    z7 = os.path.join(OUT, '7zr.exe')
    if not os.path.exists(z7):
        raise SystemExit(
            '缺少 %s（内嵌解压器）。下载 https://www.7-zip.org/a/7zr.exe 放到 setup\\out\\ 后重试。' % z7)
    compile_cs(exe, CS_SHELL,
               extra=['/resource:%s,CoverArt.png' % os.path.join(OUT, 'cover.png'),
                      '/resource:%s,7zr.exe' % z7])
    set_version_language(exe, 0)
    print('  setup-shell.exe %s' % human(os.path.getsize(exe)))


def assemble():
    print('== 拼装安装程序 ==')
    sys.path.insert(0, HERE)
    from assemble_setup import main as assemble_main
    rc = assemble_main(os.path.join(OUT, 'setup-shell.exe'), os.path.join(OUT, 'payload.7z'), SETUP_EXE)
    if rc != 0:
        raise SystemExit('拼装失败')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--skip-payload', action='store_true', help='沿用 setup\\out\\payload.7z')
    ap.add_argument('--skip-shell', action='store_true', help='沿用 setup\\out\\setup-shell.exe')
    a = ap.parse_args()

    make_icon_and_fonts()
    build_uninstaller()

    if not a.skip_payload:
        extract_electron()
        ensure_branding()
        write_app_resources()
        write_payload_root()
        compress_payload()

    arc = os.path.join(OUT, 'payload.7z')
    if not os.path.exists(arc):
        raise SystemExit('没有载荷：%s' % arc)
    write_facts()

    if not a.skip_shell:
        build_shell()

    assemble()
    print('')
    print('完成：%s' % SETUP_EXE)
    print('  大小 %s' % human(os.path.getsize(SETUP_EXE)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
