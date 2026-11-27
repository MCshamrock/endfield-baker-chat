# verify.py —— 一键验收：装 → 查 → 起应用抓图 → 卸载 → 查
#
# 用法（项目根）：
#   python setup\tools\verify.py                     # 用 setup\out 里最新的安装程序
#   python setup\tools\verify.py --exe <安装程序.exe> --dir <测试安装目录>
#
# 退出码 0 = 全部通过；1 = 有项失败（会逐条打印 FAIL）。

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.dirname(HERE)
OUT = os.path.join(SETUP, 'out')
CDP_PORT = '9340'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # client
ASAR_JS = os.path.join(ROOT, 'node_modules', '@electron', 'asar', 'bin', 'asar.js')
EXPECT_VERSION = json.load(open(os.path.join(ROOT, 'package.json'), encoding='utf-8'))['version']
CDP_EVAL = os.path.join(HERE, 'cdp-eval.mjs')
CDP_SHOT = os.path.join(HERE, 'cdp-shot.mjs')
SHOT = os.path.join(HERE, 'shot.ps1')

checks = []


def ok(name, cond, detail=''):
    checks.append((name, bool(cond), detail))
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('  <- ' + detail) if detail and not cond else ''))
    return cond


def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', default='')
    ap.add_argument('--dir', default=os.path.join(SETUP, 'mock', 'verify-install'))
    ap.add_argument('--keep', action='store_true', help='验收完不卸载')
    a = ap.parse_args()

    exe = a.exe or (sorted(glob.glob(os.path.join(OUT, 'BakerAI4-v*-Setup.exe')))[-1])
    target = os.path.abspath(a.dir)
    print('安装程序: %s (%d B)' % (exe, os.path.getsize(exe)))
    print('测试目录: %s' % target)

    if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)

    # ---- 1. 静默安装 ----
    print('\n[1] 静默安装')
    t0 = time.time()
    r = run([exe, '/S', '/D=' + target, '/DESKTOP'])
    dt = time.time() - t0
    ok('退出码 0（%.1fs）' % dt, r.returncode == 0, str(r.returncode))

    # ---- 2. 装出什么 ----
    print('\n[2] 安装结果')
    files = 0
    total = 0
    for base, _d, names in os.walk(target):
        for n in names:
            files += 1
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    # asar 打包后：Electron 运行时 ~75 个文件 + 3 个助手文件，网页层全在 app.asar 里
    ok('文件数 ≥ 70（实测 %d）' % files, files >= 70, str(files))
    for rel in ('app/Baker AI 4.exe', 'app/resources/app.asar',
                '使用说明.txt', 'Saira-OFL.txt', 'Uninstall Baker AI 4.exe'):
        ok('存在 ' + rel, os.path.exists(os.path.join(target, rel.replace('/', os.sep))))

    # 网页层在 app.asar 里：用 asar list 查 index.html 与主 JS
    asar = os.path.join(target, 'app', 'resources', 'app.asar')
    listing = ''
    try:
        listing = subprocess.run(['node', ASAR_JS, 'list', asar], capture_output=True,
                                 encoding='utf-8', errors='replace', timeout=180).stdout or ''
    except Exception as e:
        ok('asar 可读', False, str(e))
    ok('网页主包存在（asar 内 dist/index.html）', 'dist' in listing and 'index.html' in listing)
    ok('asar 内有主 JS', 'assets' in listing and '.js' in listing)

    ok('桌面快捷方式', os.path.exists(os.path.join(os.environ['USERPROFILE'], 'Desktop', 'Baker AI 4.lnk')))

    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Uninstall\BakerAI4')
        name, _ = winreg.QueryValueEx(key, 'DisplayName')
        ver, _ = winreg.QueryValueEx(key, 'DisplayVersion')
        loc, _ = winreg.QueryValueEx(key, 'InstallLocation')
        winreg.CloseKey(key)
        ok('卸载登记 %s / %s' % (name, ver), 'Baker AI 4' in name and ver == EXPECT_VERSION, ver)
        ok('安装路径一致', os.path.normcase(loc) == os.path.normcase(target), loc)
    except OSError as e:
        ok('卸载登记', False, str(e))

    # ---- 3. 起应用 ----
    print('\n[3] 启动应用')
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    env.pop('NODE_OPTIONS', None)
    app = os.path.join(target, 'app', 'Baker AI 4.exe')
    proc = subprocess.Popen([app, '--remote-debugging-port=' + CDP_PORT], env=env, cwd=target)
    time.sleep(8)

    ok('进程还在', proc.poll() is None)
    for d in ('bakerai_data', 'bakerai_logs', 'bakerai_webdata', 'bakerai_backups', 'bakerai_exports'):
        ok('数据目录 ' + d, os.path.isdir(os.path.join(target, d)))

    try:
        ev = subprocess.run(['node', CDP_EVAL, CDP_PORT,
                             "JSON.stringify({m:document.body.innerText.includes('明河'),s:typeof window.nativeStorage})"],
                            capture_output=True, text=True, timeout=60)
        val = json.loads(ev.stdout.strip().strip('"').replace('\\"', '"')) if ev.stdout.strip() else {}
        ok('页面里能读到「明河」', val.get('m') is True, ev.stdout[:120])
        ok('nativeStorage 桥存在', val.get('s') == 'object', str(val))
    except Exception as e:
        ok('CDP 检查', False, str(e))

    try:
        subprocess.run(['node', CDP_SHOT, CDP_PORT, os.path.join(OUT, 'verify-app.png'), '1200'],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(2)

    if a.keep:
        print('\n(--keep) 保留安装，跳过卸载')
        return summarize()

    # ---- 4. 静默卸载 ----
    print('\n[4] 静默卸载')
    un = os.path.join(target, 'Uninstall Baker AI 4.exe')
    r = run([un, '/quiet'])
    ok('卸载退出码 0', r.returncode == 0, str(r.returncode))
    time.sleep(6)

    ok('app\\ 已删除', not os.path.exists(os.path.join(target, 'app')))
    ok('使用说明已删除', not os.path.exists(os.path.join(target, '使用说明.txt')))
    ok('字体许可已删除', not os.path.exists(os.path.join(target, 'Saira-OFL.txt')))
    ok('卸载器已自删', not os.path.exists(un))
    ok('桌面快捷方式已删', not os.path.exists(os.path.join(os.environ['USERPROFILE'], 'Desktop', 'Baker AI 4.lnk')))
    try:
        import winreg
        winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                       r'Software\Microsoft\Windows\CurrentVersion\Uninstall\BakerAI4')
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                       r'Software\Microsoft\Windows\CurrentVersion\Uninstall\BakerAI4'))
        ok('卸载登记已删', False)
    except OSError:
        ok('卸载登记已删', True)
    ok('聊天数据仍保留', os.path.isdir(os.path.join(target, 'bakerai_data')))

    return summarize()


def summarize():
    bad = [c for c in checks if not c[1]]
    print('\n%d 项检查，%d 项失败' % (len(checks), len(bad)))
    if bad:
        for name, _c, detail in bad:
            print('  FAIL ' + name + (('  <- ' + detail) if detail else ''))
        return 1
    print('===== 全部通过 =====')
    return 0


if __name__ == '__main__':
    sys.exit(main())
