// Uninstall.cs —— 卸载程序（卸载 Baker AI 4.exe）
//
// 布局：<安装根>\app\（程序本体） + 卸载 Baker AI 4.exe + 使用说明.txt
//       <安装根>\bakerai_data|logs|webdata|backups|exports（聊天记录等，默认保留）
//
// 默认只删程序：聊天记录、设置、备份都留在安装目录；勾选后才连数据一起删。
// 删不掉自己：写一个临时 .cmd，等本进程退出后再删目录和这个 exe。
// 静默：/quiet（保留数据），退出码 0 成功 / 1 失败。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using BakerSetup;
using BakerUi;

[assembly: System.Reflection.AssemblyTitle("Baker AI 4 卸载程序")]
[assembly: System.Reflection.AssemblyProduct("Baker AI 4")]
[assembly: System.Reflection.AssemblyVersion(BakerSetup.Facts.Version + ".0")]
[assembly: System.Reflection.AssemblyFileVersion(BakerSetup.Facts.Version + ".0")]

namespace BakerUninstall
{
    internal static class Info
    {
        public const string DisplayName = "Baker AI 4";
        public const string Version = Facts.Version;
        public const string AppSubDir = "app";
        public const string Readme = "使用说明.txt";
        public const string UninstallExeName = "Uninstall Baker AI 4.exe";
        public const string UninstallKey = "BakerAI4";
        public static readonly string[] DataDirs = new string[] {
            "bakerai_data", "bakerai_logs", "bakerai_webdata", "bakerai_backups", "bakerai_exports" };
    }

    internal static class Uninstaller
    {
        public static string InstallDir = Path.GetDirectoryName(Application.ExecutablePath);
        public static string LogPath = Path.Combine(Path.GetTempPath(), "bakerai-uninstall.log");

        public static void Log(string text)
        {
            try
            {
                File.AppendAllText(LogPath,
                    "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] " + text + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch { }
        }

        public delegate void Progress(int done, int total, string name);

        /// <summary>当前进程是否已经是管理员</summary>
        public static bool IsElevated()
        {
            try
            {
                using (System.Security.Principal.WindowsIdentity id =
                    System.Security.Principal.WindowsIdentity.GetCurrent())
                {
                    System.Security.Principal.WindowsPrincipal p =
                        new System.Security.Principal.WindowsPrincipal(id);
                    return p.IsInRole(System.Security.Principal.WindowsBuiltInRole.Administrator);
                }
            }
            catch { return false; }
        }

        /// <summary>Baker AI 4 主程序是否还在运行（进程名就是 exe 名）</summary>
        public static bool IsAppRunning()
        {
            try { return Process.GetProcessesByName(Info.DisplayName).Length > 0; }
            catch { return false; }
        }

        /// <summary>结束正在运行的 Baker AI 4（先按正常退出，再强制），返回是否已全部退出</summary>
        public static bool CloseApp()
        {
            for (int round = 0; round < 3; round++)
            {
                Process[] ps;
                try { ps = Process.GetProcessesByName(Info.DisplayName); }
                catch { return false; }
                if (ps.Length == 0) return true;

                foreach (Process p in ps)
                {
                    try
                    {
                        p.CloseMainWindow();      // 先给一次正常关窗的机会
                        if (!p.WaitForExit(1200)) p.Kill();
                        p.WaitForExit(3000);
                    }
                    catch { }
                }
                System.Threading.Thread.Sleep(500);
            }
            return !IsAppRunning();
        }

        /// <summary>安装目录能不能写（装到 Program Files 时普通用户写不了，卸载也就删不掉）</summary>
        public static bool CanModifyInstallDir()
        {
            try
            {
                string probe = Path.Combine(InstallDir, ".uninstall-probe-" + Guid.NewGuid().ToString("N").Substring(0, 6));
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
                return true;
            }
            catch { return false; }
        }

        /// <summary>以管理员身份重启卸载程序：带上原来的参数（/quiet 等）再加 /ELEVATED</summary>
        public static bool RelaunchElevated(string[] args)
        {
            try
            {
                string joined = "";
                foreach (string a in args)
                {
                    string t = (a ?? "").Trim();
                    if (t.Length == 0) continue;
                    if (t.Equals("/ELEVATED", StringComparison.OrdinalIgnoreCase)) continue;
                    joined += (joined.Length > 0 ? " " : "") + (t.IndexOf(' ') >= 0 ? "\"" + t + "\"" : t);
                }
                joined += " /ELEVATED";

                ProcessStartInfo psi = new ProcessStartInfo(Application.ExecutablePath, joined.Trim());
                psi.UseShellExecute = true;
                psi.Verb = "runas";
                Process.Start(psi);
                Log("relaunch elevated: " + joined);
                return true;
            }
            catch (Exception ex)
            {
                Log("relaunch elevated FAIL " + ex.Message);
                return false;
            }
        }

        /// <summary>要删的文件总数（只统计 app\ 与说明文件，进度分母用）</summary>
        public static int CountProgramFiles()
        {
            int n = 0;
            try
            {
                string appDir = Path.Combine(InstallDir, Info.AppSubDir);
                if (Directory.Exists(appDir))
                    n += Directory.GetFiles(appDir, "*", SearchOption.AllDirectories).Length;
                if (File.Exists(Path.Combine(InstallDir, Info.Readme))) n++;
                if (File.Exists(Path.Combine(InstallDir, "Saira-OFL.txt"))) n++;
            }
            catch { }
            return n;
        }

        /// <summary>删程序文件（app\ + 使用说明）；可选删数据目录</summary>
        public static void Remove(bool deleteData, Progress onProgress)
        {
            int total = CountProgramFiles();
            if (deleteData)
            {
                foreach (string d in Info.DataDirs)
                {
                    string p = Path.Combine(InstallDir, d);
                    if (!Directory.Exists(p)) continue;
                    try { total += Directory.GetFiles(p, "*", SearchOption.AllDirectories).Length; }
                    catch { }
                }
            }
            if (total <= 0) total = 1;

            int done = 0;
            string appDirRoot = Path.Combine(InstallDir, Info.AppSubDir);

            if (Directory.Exists(appDirRoot))
            {
                List<string> files = new List<string>();
                try { files.AddRange(Directory.GetFiles(appDirRoot, "*", SearchOption.AllDirectories)); }
                catch { }
                foreach (string f in files)
                {
                    try { File.SetAttributes(f, FileAttributes.Normal); } catch { }
                    try { File.Delete(f); } catch { }
                    done++;
                    if (onProgress != null) onProgress(done, total, f);
                }
                try { DeleteDirTree(appDirRoot); } catch { }
            }

            string readme = Path.Combine(InstallDir, Info.Readme);
            if (File.Exists(readme))
            {
                try { File.Delete(readme); } catch { }
                done++;
                if (onProgress != null) onProgress(done, total, readme);
            }

            string license = Path.Combine(InstallDir, "Saira-OFL.txt");
            if (File.Exists(license))
            {
                try { File.Delete(license); } catch { }
                done++;
                if (onProgress != null) onProgress(done, total, license);
            }

            if (deleteData)
            {
                foreach (string d in Info.DataDirs)
                {
                    string p = Path.Combine(InstallDir, d);
                    if (!Directory.Exists(p)) continue;
                    List<string> files = new List<string>();
                    try { files.AddRange(Directory.GetFiles(p, "*", SearchOption.AllDirectories)); }
                    catch { }
                    foreach (string f in files)
                    {
                        try { File.SetAttributes(f, FileAttributes.Normal); } catch { }
                        try { File.Delete(f); } catch { }
                        done++;
                        if (onProgress != null) onProgress(done, total, f);
                    }
                    try { DeleteDirTree(p); } catch { }
                }
            }

            RemoveShortcuts();
            RemoveRegistry();
            Log("remove done deleteData=" + deleteData + " total=" + total);
        }

        /// <summary>自底向上删空目录（Directory.Delete 递归遇只读/占用会整段失败）</summary>
        public static void DeleteDirTree(string dir)
        {
            try
            {
                foreach (string d in Directory.GetDirectories(dir, "*", SearchOption.AllDirectories))
                {
                    try { Directory.Delete(d, true); } catch { }
                }
                Directory.Delete(dir, true);
            }
            catch { }
        }

        private static void RemoveShortcuts()
        {
            try
            {
                string keyPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + Info.UninstallKey;
                using (Microsoft.Win32.RegistryKey key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(keyPath))
                {
                    if (key == null) return;
                    string desk = key.GetValue("ShortcutDesktop") as string;
                    string menu = key.GetValue("ShortcutStartMenu") as string;
                    if (!string.IsNullOrEmpty(desk)) try { File.Delete(desk); } catch { }
                    if (!string.IsNullOrEmpty(menu))
                    {
                        try { File.Delete(menu); } catch { }
                        try
                        {
                            string dir = Path.GetDirectoryName(menu);
                            if (dir != null && Directory.Exists(dir) &&
                                Directory.GetFiles(dir).Length == 0 && Directory.GetDirectories(dir).Length == 0)
                                Directory.Delete(dir);
                        }
                        catch { }
                    }
                }
            }
            catch { }
        }

        private static void RemoveRegistry()
        {
            try
            {
                Microsoft.Win32.Registry.CurrentUser.DeleteSubKeyTree(
                    @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + Info.UninstallKey, false);
            }
            catch { }
        }

        /// <summary>
        /// 自己删不掉自己：等本进程退出后，由临时 PowerShell 脚本删掉剩下的程序和这个 exe。
        /// 用 ps1 而不是 .cmd：路径里可能有中文，.cmd 的编码（ANSI/UTF-8）会随系统 codepage 翻车。
        /// </summary>
        public static void ScheduleSelfDelete(bool deleteData)
        {
            try
            {
                string ps1 = Path.Combine(Path.GetTempPath(), "baker-uninstall-" +
                    Guid.NewGuid().ToString("N").Substring(0, 8) + ".ps1");
                string appDir = Path.Combine(InstallDir, Info.AppSubDir);
                string readme = Path.Combine(InstallDir, Info.Readme);
                string license = Path.Combine(InstallDir, "Saira-OFL.txt");
                string self = Path.Combine(InstallDir, Info.UninstallExeName);

                StringBuilder sb = new StringBuilder();
                sb.AppendLine("$ErrorActionPreference = 'SilentlyContinue'");
                sb.AppendLine("Start-Sleep -Milliseconds 1200");
                sb.AppendLine("$app = " + Q(appDir));
                sb.AppendLine("$self = " + Q(self));
                sb.AppendLine("$dir = " + Q(InstallDir));
                sb.AppendLine("$readme = " + Q(readme));
                sb.AppendLine("$license = " + Q(license));
                sb.AppendLine("# 1) 程序目录：可能被还在运行的应用占用，重试");
                sb.AppendLine("for ($i = 0; $i -lt 20; $i++) {");
                sb.AppendLine("  if (-not (Test-Path -LiteralPath $app)) { break }");
                sb.AppendLine("  Remove-Item -LiteralPath $app -Recurse -Force");
                sb.AppendLine("  Start-Sleep -Milliseconds 500");
                sb.AppendLine("}");
                sb.AppendLine("# 2) 卸载器自己：进程刚退出时文件可能还被占用，重试");
                sb.AppendLine("for ($i = 0; $i -lt 20; $i++) {");
                sb.AppendLine("  if (-not (Test-Path -LiteralPath $self)) { break }");
                sb.AppendLine("  Remove-Item -LiteralPath $self -Force");
                sb.AppendLine("  Start-Sleep -Milliseconds 500");
                sb.AppendLine("}");
                sb.AppendLine("# 3) 说明与字体许可");
                sb.AppendLine("Remove-Item -LiteralPath $readme -Force");
                sb.AppendLine("Remove-Item -LiteralPath $license -Force");
                if (deleteData)
                {
                    sb.AppendLine("# 4) 连数据一起删：整个安装目录（此时 exe 已经删掉，不会再被占用）");
                    sb.AppendLine("for ($i = 0; $i -lt 20; $i++) {");
                    sb.AppendLine("  if (-not (Test-Path -LiteralPath $dir)) { break }");
                    sb.AppendLine("  Remove-Item -LiteralPath $dir -Recurse -Force");
                    sb.AppendLine("  Start-Sleep -Milliseconds 500");
                    sb.AppendLine("}");
                }
                else
                {
                    sb.AppendLine("# 4) 只删程序：目录空了就一并删掉");
                    sb.AppendLine("if ((Get-ChildItem -LiteralPath $dir -Force | Measure-Object).Count -eq 0) {");
                    sb.AppendLine("  Remove-Item -LiteralPath $dir -Force");
                    sb.AppendLine("}");
                }
                sb.AppendLine("# 5) 兜底：自己实在删不掉，挂到 RunOnce，下次登录再删，绝不留残骸");
                sb.AppendLine("if (Test-Path -LiteralPath $self) {");
                sb.AppendLine("  New-Item -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce' -Force | Out-Null");
                sb.AppendLine("  Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce' -Name 'BakerAICleanup' -Value ('cmd /c del /f /q \"' + $self + '\"')");
                sb.AppendLine("}");
                sb.AppendLine("Remove-Item -LiteralPath $PSCommandPath -Force");
                File.WriteAllText(ps1, sb.ToString(), new UTF8Encoding(true));

                ProcessStartInfo psi = new ProcessStartInfo("powershell.exe",
                    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + ps1 + "\"");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                Process.Start(psi);
                Log("self delete scheduled " + ps1);
            }
            catch (Exception ex)
            {
                Log("self delete FAIL " + ex.Message);
            }
        }

        /// <summary>PowerShell 单引号字符串</summary>
        private static string Q(string s)
        {
            return "'" + (s ?? "").Replace("'", "''") + "'";
        }
    }

    // ------------------------------------------------------------ 界面

    internal class UninstallForm : EfForm
    {
        public enum State { Confirm, Working, Done, Failed }

        private const float Fw = 640f, Fh = 420f;
        private const float L = 32f;
        private const float R = 608f;

        public State Cur = State.Confirm;
        public bool DeleteData;
        public string ErrorText = "";
        public int FilesDone, FilesTotal;

        private EfCheck _check;
        private EfButton _cancel, _go, _closeX, _close;
        private readonly Tween _strip = new Tween(0f);
        private readonly Tween _shutter = new Tween(1f);

        public event EventHandler UninstallRequested;

        private int _progFiles;
        private int _dataDirs;
        private bool _appRunning;

        /// <summary>主程序还在运行吗（提示与按钮门禁都用它）</summary>
        public bool AppRunning { get { return _appRunning; } }

        /// <summary>重新探测一次并刷新界面</summary>
        public void RefreshRunning()
        {
            _appRunning = Uninstaller.IsAppRunning();
            Invalidate(true);
        }

        public UninstallForm()
        {
            Text = "卸载 Baker AI 4";
            Anims.Add(_strip);
            Anims.Add(_shutter);
            _progFiles = Uninstaller.CountProgramFiles();
            foreach (string d in Info.DataDirs)
                if (Directory.Exists(Path.Combine(Uninstaller.InstallDir, d))) _dataDirs++;
            _appRunning = Uninstaller.IsAppRunning();

            _closeX = new EfButton();
            _closeX.Host = this;
            _closeX.Style = EfButton.Kind.Ghost;
            _closeX.Font = EfFont.Zh(15f, true);
            _closeX.AccessibleName = "关闭卸载程序";
            _closeX.Activated += delegate { Close(); };
            Controls.Add(_closeX);

            _check = new EfCheck();
            _check.Host = this;
            _check.Text2 = "同时删除聊天记录与设置";
            _check.AccessibleName = "同时删除聊天记录与设置";
            _check.Toggled += delegate { DeleteData = _check.Checked; };
            Controls.Add(_check);

            _cancel = Make("取消", EfButton.Kind.Secondary, 15f);
            _cancel.Activated += delegate { Close(); };

            _go = Make("卸载", EfButton.Kind.Danger, 16f);
            _go.AccessibleName = "开始卸载";
            _go.Activated += delegate { if (UninstallRequested != null) UninstallRequested(this, EventArgs.Empty); };

            _close = Make("关闭", EfButton.Kind.Secondary, 15f);
            _close.Activated += delegate { Close(); };

            ApplyState();
            ActiveControl = _cancel;   // 默认落在"取消"上：破坏性操作不做默认
        }

        private EfButton Make(string text, EfButton.Kind kind, float px)
        {
            EfButton b = new EfButton();
            b.Host = this;
            b.Style = kind;
            b.Text2 = text;
            b.Font = EfFont.Zh(px, true);
            b.Visible = false;
            Controls.Add(b);
            return b;
        }

        protected override void OnLaidOut()
        {
            SetLogicalSize(Fw, Fh);
            _closeX.Place(R - 26f, 12f, 30f, 30f, S);
            _check.Place(L, 176f, 320f, 26f, S);
            _cancel.Place(392f, 352f, 96f, 48f, S);
            _go.Place(500f, 352f, 108f, 48f, S);
            _close.Place(492f, 352f, 116f, 48f, S);
            ApplyState();
        }

        public void ApplyState()
        {
            bool confirm = Cur == State.Confirm;
            _check.Visible = confirm;
            _cancel.Visible = confirm;
            _go.Visible = confirm;
            _close.Visible = Cur == State.Done || Cur == State.Failed;
            _closeX.Visible = Cur != State.Working;
            _closeX.Place(R - 26f, 12f, 30f, 30f, S);
            Invalidate(true);
        }

        public void ToWorking()
        {
            Cur = State.Working;
            ApplyState();
            _shutter.To(0f, 0f, false);
            _shutter.To(1f, 300f, !ReducedMotion);
        }

        public void ToDone()
        {
            Cur = State.Done;
            ApplyState();
            _shutter.To(0f, 0f, false);
            _shutter.To(1f, 300f, !ReducedMotion);
            if (_close.Visible) _close.Focus();
        }

        public void ToFailed(string err)
        {
            Cur = State.Failed;
            ErrorText = err ?? "";
            ApplyState();
            _shutter.To(0f, 0f, false);
            _shutter.To(1f, 300f, !ReducedMotion);
        }

        public void Report(int done, int total, string name)
        {
            FilesDone = done;
            FilesTotal = total;
            Invalidate(true);
        }

        protected override void OnEscapeKey()
        {
            if (Cur != State.Working) Close();
        }

        protected override void OnEnterKey()
        {
            // Enter 不做默认卸载：只有真的点「卸载」才动手
            if (Cur != State.Working) Close();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (Cur == State.Working) e.Cancel = true;
            base.OnFormClosing(e);
        }

        public void PlayEntrance()
        {
            if (ReducedMotion) { _strip.To(1f, 0f, false); Invalidate(true); return; }
            _strip.To(0f, 0f, false);
            _strip.To(1f, 360f, true);
        }

        // ------------------------------------------------------------ 绘制

        protected override void PaintContent(Graphics g)
        {
            Dx.Fill(g, new RectangleF(0, 0, Fw, Fh), Theme.Paper);

            if (_strip.Value > 0.001f)
            {
                float w = Fw * _strip.Value;
                Dx.Fill(g, new RectangleF(0, 0, w / 3f, Theme.Strip), Theme.TriA);
                Dx.Fill(g, new RectangleF(w / 3f, 0, w / 3f, Theme.Strip), Theme.Signal);
                Dx.Fill(g, new RectangleF(w * 2f / 3f, 0, w / 3f, Theme.Strip), Theme.TriC);
            }

            Font label = EfFont.Label(11f);
            Font title = EfFont.Zh(22f, true);

            if (Cur == State.Confirm)
            {
                Dx.Eyebrow(g, "// UNINSTALL", label, Theme.Ink2, L, 38f);
                Dx.Txt(g, "卸载 Baker AI 4", title, Theme.Ink, L, 52f);

                Font tf = EfFont.Field(12f);
                string tag = "v" + Info.Version;
                float tw = Dx.Measure(g, tag, tf) + 20f;
                Dx.FillCut(g, new RectangleF(R - tw, 56f, tw, 24f), Corner.BR, Theme.CutSmall, Theme.TagBg);
                Dx.Txt(g, tag, tf, Theme.TagFg, R - tw + 10f, 56f + (24f - tf.GetHeight(g)) / 2f);

                Dx.HLine(g, L, R, 100f, Theme.Rule);

                Dx.Txt(g, "安装位置", EfFont.Zh(12f, true), Theme.Ink2, L, 118f);
                Dx.Txt(g, Ellip(g, Uninstaller.InstallDir, EfFont.Zh(12.5f, false), R - L), EfFont.Zh(12.5f, false),
                    Theme.Ink, L, 140f);

                string notice = "只删程序时，聊天记录、设置与备份都留在安装目录。勾选上面一项会连数据一起删除，删了找不回来。";
                if (_appRunning) notice += "\n（Baker AI 4 正在运行：点「卸载」时会先请你关闭程序。）";
                Dx.Notice(g, "注意", notice,
                    EfFont.Zh(12.5f, false), EfFont.Zh(11f, true), L, 214f, R - L, _appRunning ? 84f : 58f);

                Dx.Fields(g, new string[] {
                    "程序 " + _progFiles.ToString("N0") + " 个文件",
                    "数据 " + _dataDirs + " 个目录" + (_dataDirs > 0 ? "（默认保留）" : "") },
                    EfFont.Field(11.5f), Theme.Ink2, L, _appRunning ? 322f : 306f);
            }
            else
            {
                DrawPanel(g);
            }

            using (Pen p = new Pen(Theme.Ink, 1f))
            {
                p.Alignment = PenAlignment.Inset;
                g.DrawRectangle(p, 0, 0, Fw - 1f, Fh - 1f);
            }
        }

        private void DrawPanel(Graphics g)
        {
            float h = (340f - 112f) * Math.Max(0f, Math.Min(1f, _shutter.Value));
            if (h <= 0.5f) return;
            RectangleF b = new RectangleF(L, 112f, R - L, 228f);

            GraphicsState st = g.Save();
            g.SetClip(new RectangleF(b.X, b.Y, b.Width, h), CombineMode.Intersect);
            Dx.Fill(g, b, Theme.Panel);
            Dx.Box(g, b, Theme.Ink);
            g.Restore(st);
            if (_shutter.Value < 0.55f) return;

            Font title = EfFont.Zh(22f, true);
            float x = L + 24f;

            if (Cur == State.Working)
            {
                Dx.Eyebrow(g, "// REMOVING", EfFont.Label(11f), Theme.Ink2, x, 134f);
                Dx.Txt(g, "正在卸载", title, Theme.Ink, x, 148f);

                int pct = FilesTotal > 0 ? (int)Math.Round(FilesDone * 100.0 / FilesTotal) : 0;
                if (pct > 100) pct = 100;
                Dx.TxtRight(g, pct.ToString("000") + "%", EfFont.Display(40f), Theme.Ink, R - 24f, 132f);

                float bx = x, bw = R - L - 48f;
                float segGap = 3f;
                float segW = (bw - segGap * 23f) / 24f;
                int lit = (int)Math.Floor(pct / 100.0 * 24.0 + 0.0001);
                for (int i = 0; i < 24; i++)
                {
                    RectangleF r = new RectangleF(bx + i * (segW + segGap), 200f, segW, 16f);
                    Dx.Fill(g, r, i < lit ? Theme.Signal : Theme.Chip);
                }
                Dx.Txt(g, "正在删除 " + FilesDone.ToString("N0") + " / " + Math.Max(FilesTotal, 0).ToString("N0") + " 个文件",
                    EfFont.Zh(12.5f, false), Theme.Ink2, bx, 232f);
                Dx.Txt(g, "请不要关闭窗口。", EfFont.Zh(11.5f, false), Theme.Ink2, bx, 258f);
                return;
            }

            if (Cur == State.Done)
            {
                Dx.Eyebrow(g, "// DONE", EfFont.Label(11f), Theme.Ink2, x, 134f);
                Dx.Txt(g, "卸载完成", title, Theme.Ink, x, 148f);

                RectangleF ck = new RectangleF(R - 76f, 134f, 48f, 48f);
                Dx.FillCut(g, ck, Corner.BR, 8f, Theme.Signal);
                using (Pen p = new Pen(Theme.Ink, 4f))
                {
                    p.StartCap = LineCap.Flat;
                    p.EndCap = LineCap.Flat;
                    p.LineJoin = LineJoin.Miter;
                    g.DrawLines(p, new PointF[] {
                        new PointF(ck.X + 12f, ck.Y + 25f),
                        new PointF(ck.X + 21f, ck.Y + 35f),
                        new PointF(ck.X + 37f, ck.Y + 13f) });
                }

                string note = DeleteData
                    ? "程序与聊天记录都已删除。"
                    : "程序已删除，聊天记录与备份仍保留在安装目录。";
                Dx.Txt(g, note, EfFont.Zh(13f, false), Theme.Ink, x, 218f);

                Dx.Fields(g, new string[] {
                    DeleteData ? "数据已删除" : "数据已保留",
                    DeleteData ? "安装目录已清空" : "安装目录只留 bakerai_*" },
                    EfFont.Field(11.5f), Theme.Ink2, x, 252f);
                return;
            }

            Dx.Eyebrow(g, "// FAILED", EfFont.Label(11f), Theme.Ink2, x, 134f);
            Dx.Txt(g, "卸载失败", title, Theme.Ink, x, 148f);
            string err = string.IsNullOrEmpty(ErrorText) ? "删除过程中出错，请稍后重试。" : ErrorText;
            Dx.Notice(g, "出错", err, EfFont.Zh(12.5f, false), EfFont.Zh(11f, true),
                x, 196f, R - L - 48f, 96f);
        }

        private static string Ellip(Graphics g, string s, Font f, float maxW)
        {
            if (string.IsNullOrEmpty(s)) return s;
            if (Dx.Measure(g, s, f) <= maxW) return s;
            string cut = s;
            while (cut.Length > 1 && Dx.Measure(g, cut + "…", f) > maxW) cut = cut.Substring(0, cut.Length - 1);
            return cut + "…";
        }
    }

    // ------------------------------------------------------------ 入口

    internal static class Program
    {
        /// <summary>
        /// 主程序还在运行：弹自己的确认框，让用户在这里直接把它关掉。
        /// 返回 true 表示已经关掉、可以继续卸载；false 表示还拦着。
        /// </summary>
        private static bool EnsureAppClosed(Form owner)
        {
            if (!Uninstaller.IsAppRunning()) return true;

            bool go = false;
            using (PromptForm p = new PromptForm("// RUNNING", "Baker AI 4 正在运行",
                "卸载前需要先关闭正在运行的程序。\n\n" +
                "点「先关闭程序」会结束 Baker AI 4 进程，然后继续卸载；聊天记录是自动保存的，正在生成的回复会中断。",
                new string[] { "先关闭程序", "取消" }))
            {
                p.PlayEntrance();
                if (owner != null) p.ShowDialog(owner); else p.ShowDialog();
                go = p.Selected == 0;
            }
            if (!go) return false;

            Uninstaller.Log("closing running app");
            if (Uninstaller.CloseApp())
            {
                Uninstaller.Log("app closed");
                return true;
            }

            using (PromptForm p = new PromptForm("// RUNNING", "程序还没关掉",
                "还有 Baker AI 4 进程没有退出，请先在任务管理器里结束它，再回来卸载。",
                new string[] { "知道了" }))
            {
                p.PlayEntrance();
                if (owner != null) p.ShowDialog(owner); else p.ShowDialog();
            }
            return false;
        }

        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string theme = Environment.GetEnvironmentVariable("BAKER_SETUP_THEME");
            if (theme != null && theme.Equals("dark", StringComparison.OrdinalIgnoreCase)) Theme.SetDark();
            EfFont.Init();

            bool quiet = false;
            foreach (string a in args)
                if (a.Trim().Equals("/quiet", StringComparison.OrdinalIgnoreCase)) quiet = true;

            // 装到 Program Files 这类目录时，普通用户删不掉——先提权，再干活
            if (!Uninstaller.IsElevated() && !Uninstaller.CanModifyInstallDir())
            {
                Uninstaller.Log("install dir needs admin: " + Uninstaller.InstallDir);
                if (quiet)
                {
                    Uninstaller.Log("quiet uninstall aborted (need admin)");
                    return 1;
                }

                bool go = false;
                using (PromptForm p = new PromptForm("// ADMIN", "需要管理员权限",
                    "这个安装目录需要管理员权限才能删除：\n" + Uninstaller.InstallDir +
                    "\n\n是否以管理员身份重新启动卸载程序？",
                    new string[] { "以管理员身份重启", "取消" }))
                {
                    p.PlayEntrance();
                    p.ShowDialog();
                    go = p.Selected == 0;
                }
                if (!go) return 1;
                return Uninstaller.RelaunchElevated(args) ? 0 : 1;
            }

            if (quiet)
            {
                if (Uninstaller.IsAppRunning())
                {
                    // 静默模式不替用户杀进程：让自动化自己先关掉程序
                    Uninstaller.Log("quiet uninstall aborted (app running)");
                    return 1;
                }
                try
                {
                    Uninstaller.Remove(false, null);
                    Uninstaller.ScheduleSelfDelete(false);
                    return 0;
                }
                catch (Exception ex)
                {
                    Uninstaller.Log("quiet FAIL " + ex.Message);
                    return 1;
                }
            }

            bool removed = false;
            bool removedData = false;
            UninstallForm form = new UninstallForm();
            form.UninstallRequested += delegate
            {
                // 主程序还在跑就不让卸：先弹窗把它关掉
                if (form.AppRunning || Uninstaller.IsAppRunning())
                {
                    form.RefreshRunning();
                    if (!EnsureAppClosed(form)) return;
                    form.RefreshRunning();
                }
                form.ToWorking();
                bool deleteData = form.DeleteData;
                ThreadPool.QueueUserWorkItem(delegate
                {
                    try
                    {
                        Uninstaller.Remove(deleteData,
                            delegate(int done, int total, string name)
                            {
                                try
                                {
                                    if (form.IsHandleCreated && !form.IsDisposed)
                                        form.BeginInvoke(new Action(delegate { form.Report(done, total, name); }));
                                }
                                catch { }
                            });
                        try
                        {
                            if (form.IsHandleCreated && !form.IsDisposed)
                                form.BeginInvoke(new Action(delegate { form.ToDone(); }));
                        }
                        catch { }
                        removed = true;
                        removedData = deleteData;
                    }
                    catch (Exception ex)
                    {
                        string msg = ex.Message;
                        try
                        {
                            if (form.IsHandleCreated && !form.IsDisposed)
                                form.BeginInvoke(new Action(delegate { form.ToFailed(msg); }));
                        }
                        catch { }
                    }
                });
            };
            form.Shown += delegate
            {
                form.PlayEntrance();
                if (form.AppRunning && EnsureAppClosed(form)) form.RefreshRunning();
            };
            Application.Run(form);

            // 窗体关掉、进程要退的时候才安排自删：卸载器 exe 这时才可能被删
            if (removed) Uninstaller.ScheduleSelfDelete(removedData);
            return 0;
        }
    }
}
