// Installer.cs —— 安装逻辑与入口
//
// 文件结构：[ setup-shell.exe ][ payload.7z ][ footer ]
//   footer = "BAKERSET" + Int64(载荷偏移) + Int64(载荷长度)   （小端，24 字节）
// 解压调用 Windows 自带的 tar.exe（bsdtar 原生支持 7z），不随包分发第三方解压器。

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using BakerUi;

[assembly: AssemblyTitle("Baker AI 4 安装程序")]
[assembly: AssemblyProduct("Baker AI 4")]
[assembly: AssemblyVersion(BakerSetup.Facts.Version + ".0")]
[assembly: AssemblyFileVersion(BakerSetup.Facts.Version + ".0")]

namespace BakerSetup
{
    /// <summary>产品信息：版本号统一取 Facts.cs（build.py 按 package.json 生成），别在这里写死</summary>
    internal static class AppInfo
    {
        public const string DisplayName = "Baker AI 4";
        public const string ShortcutName = "Baker AI 4";
        public const string Version = Facts.Version;
        public const string DefaultDirName = "Baker AI 4";
        public const string AppSubDir = "app";
        public const string AppExe = "Baker AI 4.exe";
        public const string UninstallExe = "Uninstall Baker AI 4.exe";
        public const string Readme = "使用说明.txt";
        public const string UninstallKey = "BakerAI4";
        public const string Publisher = "Baker AI 4";
    }

    internal static class Installer
    {
        public const string Magic = "BAKERSET";

        public static string ExePath = Application.ExecutablePath;
        public static string LogPath = Path.Combine(Path.GetTempPath(), "bakerai-setup.log");
        public static long PayloadOffset, PayloadLength;
        public static bool Failed;

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

        // ------------------------------------------------------------ 页脚

        public static bool ReadFooter()
        {
            try
            {
                using (FileStream fs = File.OpenRead(ExePath))
                {
                    if (fs.Length < 24) return false;
                    fs.Seek(-24, SeekOrigin.End);
                    byte[] b = new byte[24];
                    int got = 0;
                    while (got < 24)
                    {
                        int n = fs.Read(b, got, 24 - got);
                        if (n <= 0) break;
                        got += n;
                    }
                    if (got < 24) return false;
                    if (Encoding.ASCII.GetString(b, 0, 8) != Magic) return false;
                    long off = BitConverter.ToInt64(b, 8);
                    long len = BitConverter.ToInt64(b, 16);
                    if (off < 24 || len <= 0) return false;
                    if (off + len + 24 != fs.Length) return false;
                    PayloadOffset = off;
                    PayloadLength = len;
                    return true;
                }
            }
            catch (Exception ex)
            {
                Log("ReadFooter FAIL " + ex.Message);
                return false;
            }
        }

        // ------------------------------------------------------------ tar

        private static string TarPath()
        {
            string p = Path.Combine(Environment.SystemDirectory, "tar.exe");
            return File.Exists(p) ? p : "tar.exe";
        }

        private static ProcessStartInfo TarStart(string args)
        {
            ProcessStartInfo psi = new ProcessStartInfo(TarPath(), args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            // bsdtar 把文件名按系统 ANSI 代码页输出（中文系统是 GBK），
            // 这里跟着用 Encoding.Default，别硬当 UTF-8，否则中文文件名变成一串 �
            psi.StandardOutputEncoding = Encoding.Default;
            psi.StandardErrorEncoding = Encoding.Default;
            return psi;
        }

        // ------------------------------------------------------------ 内嵌 7zr
        //
        // 为什么要有它：解压原本只靠系统自带的 tar.exe（bsdtar 原生支持 7z），但
        //   · Windows 10 1803 以前 / 精简系统里根本没有 tar.exe；
        //   · 装了 Git for Windows 的机器，PATH 里的 GNU tar 会被当成 tar.exe 调用，
        //     它不认 7z，输出一行错误就退出 —— 表现为「载荷清单为空」，安装直接失败。
        // 所以把 7zr.exe（x86 独立版，600 KB，任何 Windows 都能跑）编进外壳里当兜底。

        private static string sevenZipExe;
        private static bool sevenZipReady;

        /// <summary>本次安装是否改走内嵌 7zr（tar 不可用 / 手动 /7ZR）</summary>
        public static bool UseSevenZip;

        private static string SevenZip()
        {
            if (sevenZipReady) return sevenZipExe;
            sevenZipReady = true;
            try
            {
                string dir = Path.Combine(Path.GetTempPath(), "baker-7zr");
                Directory.CreateDirectory(dir);
                string exe = Path.Combine(dir, "7zr.exe");
                FileInfo fi = new FileInfo(exe);
                if (!fi.Exists || fi.Length < 300000)
                {
                    using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("7zr.exe"))
                    {
                        if (s == null) return null;
                        using (FileStream f = new FileStream(exe, FileMode.Create, FileAccess.Write))
                        {
                            byte[] buf = new byte[64 * 1024];
                            int n;
                            while ((n = s.Read(buf, 0, buf.Length)) > 0) f.Write(buf, 0, n);
                        }
                    }
                }
                sevenZipExe = exe;
            }
            catch (Exception ex)
            {
                Log("7zr 释放失败 " + ex.Message);
                sevenZipExe = null;
            }
            return sevenZipExe;
        }

        private static ProcessStartInfo SevenZipStart(string args)
        {
            ProcessStartInfo psi = new ProcessStartInfo(SevenZip(), args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;   // 配合 -sccUTF-8
            psi.StandardErrorEncoding = Encoding.UTF8;
            return psi;
        }

        /// <summary>用内嵌 7zr 解压；-bb1 时每条目输出一行 "- 相对路径"，据此推进进度</summary>
        private static void ExtractWith7z(string archive, string target, int total, ProgressEntry onEntry)
        {
            string exe = SevenZip();
            if (exe == null) throw new InvalidOperationException("没有可用的解压器：系统 tar 不可用，内嵌 7zr 也释放失败");
            using (Process p = Process.Start(SevenZipStart(
                "x -y -bb1 -sccUTF-8 -o\"" + target + "\" \"" + archive + "\"")))
            {
                int done = 0;
                object gate = new object();
                StringBuilder err = new StringBuilder();
                DataReceivedEventHandler onLine = delegate(object s, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    string line = e.Data;
                    if (!line.StartsWith("- ", StringComparison.Ordinal)) return;
                    int cur;
                    lock (gate) { cur = ++done; }
                    if (onEntry != null) onEntry(Math.Min(cur, total), total, PrettyEntry(line.Substring(2)));
                };
                DataReceivedEventHandler onErr = delegate(object s, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    lock (gate) { if (err.Length < 4000) err.AppendLine(e.Data); }
                };
                p.OutputDataReceived += onLine;
                p.ErrorDataReceived += onErr;
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                p.WaitForExit();
                string errText = "";
                lock (gate) { errText = err.ToString(); }
                Log("7zr x exit=" + p.ExitCode + " entries=" + done +
                    (p.ExitCode != 0 ? " err=" + Short(errText) : ""));
                if (p.ExitCode != 0)
                    throw new InvalidOperationException("解压失败（7zr 退出码 " + p.ExitCode + "）：" +
                        (Short(errText).Length > 0 ? Short(errText) : "安装包可能损坏，请重新下载"));
            }
        }

        /// <summary>载荷条目数 = 进度分母（优先 tar -tf，失败则用内嵌 7zr）</summary>
        public static int CountEntries(string payloadFile)
        {
            // /7ZR 已显式指定，或上一轮已知 tar 不可用：直接用 7zr
            if (UseSevenZip) return CountWith7z(payloadFile);
            int n = CountWithTar(payloadFile);
            if (n > 0) return n;
            // 系统 tar 不可用 / 不认 7z（老系统没 tar、PATH 里是 GNU tar、被安全软件拦）时兜底
            int m = CountWith7z(payloadFile);
            if (m > 0)
            {
                UseSevenZip = true;
                Log("系统 tar 清单为空，改用内嵌 7zr");
            }
            return m;
        }

        private static int CountWithTar(string payloadFile)
        {
            try
            {
                ProcessStartInfo psi = TarStart("-tf \"" + payloadFile + "\"");
                using (Process p = Process.Start(psi))
                {
                    int n = 0;
                    using (StreamReader r = p.StandardOutput)
                    {
                        string line;
                        while ((line = r.ReadLine()) != null)
                            if (line.Trim().Length > 0) n++;
                    }
                    string err = p.StandardError.ReadToEnd();
                    p.WaitForExit();
                    if (n == 0) Log("tar -tf 无输出：exit=" + p.ExitCode + " err=" + Short(err));
                    return n;
                }
            }
            catch (Exception ex)
            {
                Log("tar 启动失败 " + ex.Message);
                return 0;
            }
        }

        private static int CountWith7z(string payloadFile)
        {
            if (SevenZip() == null) return 0;
            try
            {
                using (Process p = Process.Start(SevenZipStart("l -slt -sccUTF-8 \"" + payloadFile + "\"")))
                {
                    int n = 0;
                    bool first = true;
                    using (StreamReader r = p.StandardOutput)
                    {
                        string line;
                        while ((line = r.ReadLine()) != null)
                        {
                            if (!line.StartsWith("Path = ", StringComparison.Ordinal)) continue;
                            if (first) { first = false; continue; }   // 第一条是压缩包自身
                            n++;
                        }
                    }
                    string err = p.StandardError.ReadToEnd();
                    p.WaitForExit();
                    Log("7zr l exit=" + p.ExitCode + " entries=" + n +
                        (p.ExitCode != 0 ? " err=" + Short(err) : ""));
                    return p.ExitCode == 0 ? n : 0;
                }
            }
            catch (Exception ex)
            {
                Log("7zr 启动失败 " + ex.Message);
                return 0;
            }
        }

        /// <summary>只取第一行、截断，日志不至于被 tar 的长错误刷屏</summary>
        private static string Short(string s)
        {
            if (s == null) return "";
            s = s.Replace("\r", " ").Replace("\n", " ").Trim();
            return s.Length > 200 ? s.Substring(0, 200) + "…" : s;
        }

        public delegate void ProgressBytes(long done, long total);
        public delegate void ProgressEntry(int done, int total, string name);

        // ------------------------------------------------------------ 安装主流程

        public static void Install(string target, bool desktop, bool startMenu,
            ProgressBytes onBytes, ProgressEntry onEntry)
        {
            string tmp = Path.Combine(Path.GetTempPath(),
                "baker-payload-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" +
                Guid.NewGuid().ToString("N").Substring(0, 6) + ".7z");
            try
            {
                // ① 载荷从 exe 抠到临时文件（1 MB 缓冲，按字节上报）
                using (FileStream src = new FileStream(ExePath, FileMode.Open, FileAccess.Read, FileShare.Read))
                using (FileStream dst = new FileStream(tmp, FileMode.Create, FileAccess.Write))
                {
                    src.Seek(PayloadOffset, SeekOrigin.Begin);
                    byte[] buf = new byte[1024 * 1024];
                    long remain = PayloadLength, copied = 0;
                    int n;
                    while (remain > 0 && (n = src.Read(buf, 0, (int)Math.Min(buf.Length, remain))) > 0)
                    {
                        dst.Write(buf, 0, n);
                        remain -= n;
                        copied += n;
                        if (onBytes != null) onBytes(copied, PayloadLength);
                    }
                    if (remain != 0) throw new IOException("载荷读取不完整（还差 " + remain + " 字节）");
                }
                Log("payload landed " + tmp + " (" + PayloadLength + " B)");
                Log("os " + Environment.OSVersion.VersionString + " / tar " + TarPath() +
                    (File.Exists(TarPath()) ? "" : "（不存在）") + " / 7zr " + (SevenZip() ?? "不可用"));

                // ② 进度分母
                int total = CountEntries(tmp);
                if (total <= 0) throw new InvalidDataException("载荷清单为空");
                Log("entries " + total);

                // ③ 解压：默认 bsdtar（-v 清单写到 stderr，两个流都计数）；tar 不可用时用内嵌 7zr
                if (!Directory.Exists(target)) Directory.CreateDirectory(target);
                if (UseSevenZip)
                {
                    ExtractWith7z(tmp, target, total, onEntry);
                }
                else
                {
                    using (Process p = Process.Start(TarStart("-xvf \"" + tmp + "\" -C \"" + target + "\"")))
                    {
                        int done = 0;
                        object gate = new object();
                        DataReceivedEventHandler onLine = delegate(object s, DataReceivedEventArgs e)
                        {
                            if (e.Data == null) return;
                            int cur;
                            lock (gate) { cur = ++done; }
                            if (onEntry != null) onEntry(cur, total, PrettyEntry(e.Data));
                        };
                        p.OutputDataReceived += onLine;
                        p.ErrorDataReceived += onLine;
                        p.BeginOutputReadLine();
                        p.BeginErrorReadLine();
                        p.WaitForExit();
                        if (p.ExitCode != 0)
                        {
                            // tar 中途失败（含「不认 7z」的情况）：换内嵌 7zr 再来一次
                            Log("tar -xvf 失败 exit=" + p.ExitCode + "，改用内嵌 7zr 重试");
                            UseSevenZip = true;
                            ExtractWith7z(tmp, target, total, onEntry);
                        }
                    }
                }

                // ④ 校验关键文件
                string appExe = Path.Combine(target, AppInfo.AppSubDir, AppInfo.AppExe);
                // 2026-11-21：载荷改成 resources\app.asar（未打包目录在 Win11 上会触发
                // Electron CHECK 崩溃）；两种布局都认，方便回滚。
                string asar = Path.Combine(target, AppInfo.AppSubDir, "resources", "app.asar");
                string indexHtml = Path.Combine(target, AppInfo.AppSubDir, "resources", "app", "dist", "index.html");
                if (!File.Exists(appExe)) throw new FileNotFoundException("程序本体缺失：" + appExe);
                if (!File.Exists(asar) && !File.Exists(indexHtml))
                    throw new FileNotFoundException("网页资源缺失：" + asar);
                Log("verified app ok");

                // ⑤ 快捷方式
                string deskLnk = null, menuLnk = null;
                if (desktop)
                {
                    deskLnk = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                        AppInfo.ShortcutName + ".lnk");
                    CreateShortcut(deskLnk, appExe, target);
                }
                if (startMenu)
                {
                    string menuDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                        AppInfo.ShortcutName);
                    Directory.CreateDirectory(menuDir);
                    menuLnk = Path.Combine(menuDir, AppInfo.ShortcutName + ".lnk");
                    CreateShortcut(menuLnk, appExe, target);
                }
                Log("shortcuts desk=" + (deskLnk != null) + " menu=" + (menuLnk != null));

                // ⑥ 卸载登记
                RegisterUninstall(target, deskLnk, menuLnk);

                // ⑦ 使用说明追加安装信息
                AppendInstallInfo(target);

                Log("OK target=" + target);
            }
            catch (Exception ex)
            {
                Log("FAIL " + ex.Message);
                throw;
            }
            finally
            {
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
            }
        }

        private static string PrettyEntry(string raw)
        {
            string s = raw.Trim();
            if (s.Length > 2 && s[1] == ' ') s = s.Substring(2);
            return s;
        }

        // ------------------------------------------------------------ 快捷方式

        public static void CreateShortcut(string lnkPath, string target, string workDir)
        {
            // 快捷方式只是锦上添花：某些机器上 WScript.Shell 这个 COM 组件会被策略/安全软件
            // 拦掉（Activator.CreateInstance 直接抛 TargetInvocationException）。以前这里
            // 一抛就把整次安装判失败（文件都装好了、注册表却没写），现在改成尽力而为。
            try
            {
                Type tShell = Type.GetTypeFromProgID("WScript.Shell");
                if (tShell == null)
                {
                    Log("CreateShortcut: 没有 WScript.Shell，跳过快捷方式 " + lnkPath);
                    return;
                }
                object shell = Activator.CreateInstance(tShell);
                object lnk = tShell.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnkPath });
                Type t = lnk.GetType();
                t.InvokeMember("TargetPath", BindingFlags.SetProperty, null, lnk, new object[] { target });
                t.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, lnk, new object[] { workDir });
                t.InvokeMember("IconLocation", BindingFlags.SetProperty, null, lnk,
                    new object[] { target + ",0" });
                t.InvokeMember("Description", BindingFlags.SetProperty, null, lnk, new object[] { AppInfo.DisplayName });
                t.InvokeMember("Save", BindingFlags.InvokeMethod, null, lnk, null);
                System.Runtime.InteropServices.Marshal.ReleaseComObject(lnk);
                System.Runtime.InteropServices.Marshal.ReleaseComObject(shell);
            }
            catch (Exception ex)
            {
                // 装是装好了，只是没建快捷方式 —— 记一条日志，别让用户以为安装失败
                Log("CreateShortcut FAIL " + lnkPath + " : " + ex.Message);
            }
        }

        // ------------------------------------------------------------ 卸载登记

        private static void RegisterUninstall(string target, string deskLnk, string menuLnk)
        {
            string keyPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + AppInfo.UninstallKey;
            using (Microsoft.Win32.RegistryKey key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(keyPath))
            {
                string uninstaller = Path.Combine(target, AppInfo.UninstallExe);
                key.SetValue("DisplayName", AppInfo.DisplayName);
                key.SetValue("DisplayVersion", AppInfo.Version);
                key.SetValue("Publisher", AppInfo.Publisher);
                key.SetValue("InstallLocation", target);
                key.SetValue("DisplayIcon", Path.Combine(target, AppInfo.AppSubDir, AppInfo.AppExe));
                key.SetValue("UninstallString", "\"" + uninstaller + "\"");
                key.SetValue("QuietUninstallString", "\"" + uninstaller + "\" /quiet");
                long kb = DirSize(target) / 1024;
                if (kb > int.MaxValue) kb = int.MaxValue;
                key.SetValue("EstimatedSize", (int)kb, Microsoft.Win32.RegistryValueKind.DWord);
                key.SetValue("NoModify", 1, Microsoft.Win32.RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, Microsoft.Win32.RegistryValueKind.DWord);
                if (deskLnk != null) key.SetValue("ShortcutDesktop", deskLnk);
                if (menuLnk != null) key.SetValue("ShortcutStartMenu", menuLnk);
            }
            Log("registered " + keyPath);
        }

        public static long DirSize(string dir)
        {
            long sum = 0;
            try
            {
                foreach (string f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories))
                    try { sum += new FileInfo(f).Length; } catch { }
            }
            catch { }
            return sum;
        }

        private static void AppendInstallInfo(string target)
        {
            try
            {
                string readme = Path.Combine(target, AppInfo.Readme);
                string text = Environment.NewLine +
                    "—— 安装信息 ——" + Environment.NewLine +
                    "安装时间：" + DateTime.Now.ToString("yyyy-MM-dd HH:mm") + Environment.NewLine +
                    "安装路径：" + target + Environment.NewLine +
                    "版本：" + AppInfo.DisplayName + " v" + AppInfo.Version + Environment.NewLine +
                    "启动：桌面「" + AppInfo.ShortcutName + "」或 " +
                        Path.Combine(target, AppInfo.AppSubDir, AppInfo.AppExe) + Environment.NewLine +
                    "卸载：设置 → 应用 → " + AppInfo.DisplayName + "，或运行安装目录下的「" + AppInfo.UninstallExe + "」" +
                    Environment.NewLine;
                File.AppendAllText(readme, text, Encoding.UTF8);
            }
            catch (Exception ex)
            {
                Log("append readme FAIL " + ex.Message);
            }
        }

        // ------------------------------------------------------------ 目录可写探测

        public static bool ProbeWritable(string dir, out string err)
        {
            bool denied;
            return ProbeWritable(dir, out err, out denied);
        }

        /// <summary>不可写时区分「需要管理员权限」和「其它原因」，前者可以提权重启</summary>
        public static bool ProbeWritable(string dir, out string err, out bool accessDenied)
        {
            err = "";
            accessDenied = false;
            try
            {
                if (!Directory.Exists(dir)) Directory.CreateDirectory(dir);
                string probe = Path.Combine(dir, ".write-probe-" + Guid.NewGuid().ToString("N").Substring(0, 6));
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
                return true;
            }
            catch (UnauthorizedAccessException)
            {
                accessDenied = true;
                err = "这个位置需要管理员权限";
                return false;
            }
            catch (System.Security.SecurityException)
            {
                accessDenied = true;
                err = "这个位置需要管理员权限";
                return false;
            }
            catch (Exception ex)
            {
                err = "这个位置不可写，请换一个目录（" + ex.GetType().Name + "）";
                return false;
            }
        }

        /// <summary>
        /// 用户选了「D:\」这种目录时，装到它下面的 Baker AI 4 文件夹：
        /// D:\ → D:\Baker AI 4；D:\Games → D:\Games\Baker AI 4；已经是 Baker AI 4 就原样用。
        /// </summary>
        public static string NormalizeTarget(string dir)
        {
            string d = (dir ?? "").Trim().Trim('"').Trim();
            if (d.Length == 0) return d;
            d = d.TrimEnd('\\', '/');
            if (d.Length == 2 && d[1] == ':') d += "\\";        // "D:" -> "D:\"
            if (d.Length == 0) return d;

            string leaf = Path.GetFileName(d);
            if (string.Equals(leaf, AppInfo.DefaultDirName, StringComparison.OrdinalIgnoreCase))
                return d;
            return d.TrimEnd('\\') + "\\" + AppInfo.DefaultDirName;
        }

        /// <summary>读注册表里的旧安装（目录存在且程序还在才算）</summary>
        public static bool FindInstalled(out string dir, out string version)
        {
            dir = "";
            version = "";
            try
            {
                string keyPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + AppInfo.UninstallKey;
                using (Microsoft.Win32.RegistryKey key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(keyPath))
                {
                    if (key == null) return false;
                    dir = (key.GetValue("InstallLocation") as string) ?? "";
                    version = (key.GetValue("DisplayVersion") as string) ?? "";
                }
                if (dir.Length == 0 || !Directory.Exists(dir)) return false;
                if (!File.Exists(Path.Combine(dir, AppInfo.AppSubDir, AppInfo.AppExe))) return false;
                return true;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>以管理员身份重新启动自己：只重启，不复制任何东西</summary>
        public static bool RelaunchElevated(string target, bool desktop, bool startMenu, bool autoStart)
        {
            try
            {
                string args = "\"/D=" + target.TrimEnd('\\') + "\"";
                if (desktop) args += " /DESKTOP";
                if (startMenu) args += " /STARTMENU";
                if (autoStart) args += " /AUTO";
                // 路径已经是最终目录（可能来自旧安装的自定义目录），子进程别再追加 Baker AI 4
                args += " /RAW";
                ProcessStartInfo psi = new ProcessStartInfo(Application.ExecutablePath, args);
                psi.UseShellExecute = true;
                psi.Verb = "runas";
                Process.Start(psi);
                Log("relaunch elevated: " + args);
                return true;
            }
            catch (Exception ex)
            {
                Log("relaunch elevated FAIL " + ex.Message);
                return false;
            }
        }
    }

    // ------------------------------------------------------------ 入口

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            // 从带 Electron 变量的环境里启动时先剔除，否则 BakerAI.exe 会退化成 Node
            Environment.SetEnvironmentVariable("ELECTRON_RUN_AS_NODE", null);
            Environment.SetEnvironmentVariable("NODE_OPTIONS", null);

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string theme = Environment.GetEnvironmentVariable("BAKER_SETUP_THEME");
            if (theme != null && theme.Equals("dark", StringComparison.OrdinalIgnoreCase)) Theme.SetDark();
            EfFont.Init();

            bool silent = HasFlag(args, "/S");
            string dirArg = ArgValue(args, "/D=");
            bool desktop = HasFlag(args, "/DESKTOP");
            bool startMenu = HasFlag(args, "/STARTMENU");
            // 诊断用：强制走内嵌 7zr（支持排查「系统 tar 不认 7z」那类机器）
            if (HasFlag(args, "/7ZR")) Installer.UseSevenZip = true;

            if (!Installer.ReadFooter())
            {
                Installer.Log("footer invalid, abort");
                if (silent) return 1;
                MessageBox.Show("安装包不完整（页脚校验失败），请重新获取。", AppInfo.DisplayName,
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 1;
            }

            string defaultDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Programs", AppInfo.DefaultDirName);

            if (silent)
            {
                string t = dirArg.Length > 0 ? dirArg : defaultDir;
                Installer.Log("silent start target=" + t);
                try
                {
                    Installer.Install(t, desktop, startMenu, null, null);
                    return 0;
                }
                catch (Exception ex)
                {
                    Installer.Log("silent FAIL " + ex.Message);
                    return 1;
                }
            }

            SetupForm form = new SetupForm();
            string envDir = Environment.GetEnvironmentVariable("BAKER_SETUP_DIR");
            string startDir = dirArg.Length > 0 ? dirArg : (envDir != null && envDir.Length > 0 ? envDir : defaultDir);
            form.SetPath(startDir);

            // 已装过的旧版本（目录还在、程序还在才算）
            string oldDir, oldVersion;
            bool hasOld = Installer.FindInstalled(out oldDir, out oldVersion);

            form.StartRequested += delegate { BeginInstall(form); };
            form.CloseRequested += delegate { form.Close(); };
            form.RunRequested += delegate
            {
                try
                {
                    string exe = Path.Combine(form.TargetDir, AppInfo.AppSubDir, AppInfo.AppExe);
                    Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = Path.GetDirectoryName(exe) });
                }
                catch (Exception ex) { Installer.Log("run FAIL " + ex.Message); }
                form.Close();
            };
            form.ReadmeRequested += delegate
            {
                try
                {
                    string readme = Path.Combine(form.TargetDir, AppInfo.Readme);
                    if (File.Exists(readme))
                        Process.Start(new ProcessStartInfo(readme) { UseShellExecute = true });
                    else
                        Process.Start(new ProcessStartInfo("explorer.exe") { Arguments = "\"" + form.TargetDir + "\"" });
                }
                catch (Exception ex) { Installer.Log("open readme FAIL " + ex.Message); }
            };

            bool autoStart = HasFlag(args, "/AUTO") || Environment.GetEnvironmentVariable("BAKER_SETUP_AUTOSTART") == "1";

            form.Shown += delegate
            {
                form.PlayEntrance();
                if (autoStart)
                {
                    // /AUTO 是提权重启后的子进程：/RAW 表示路径已定稿，不再追加目录名，也不再二次询问提权
                    BeginInstall(form, !HasFlag(args, "/RAW"), false);
                    return;
                }
                if (hasOld)
                {
                    // 旧版本提示：默认就是「保留数据更新」
                    string body = "已安装版本：" + (oldVersion.Length > 0 ? oldVersion : "未知") + "\n" +
                                  "安装位置：" + oldDir + "\n\n" +
                                  "保留数据更新只会替换程序文件；聊天记录、设置与备份都留在原处。";
                    using (PromptForm p = new PromptForm("// UPDATE", "检测到已安装版本",
                        body, new string[] { "保留数据更新", "换个目录", "取消" }))
                    {
                        p.PlayEntrance();
                        p.ShowDialog(form);
                        if (p.Selected == 0)
                        {
                            form.SetPath(oldDir);
                            BeginInstall(form, false);   // 路径取自注册表，别再追加目录名
                        }
                        else if (p.Selected == 1)
                        {
                            form.SetPath(defaultDir);
                        }
                        else
                        {
                            form.Close();
                        }
                    }
                }
            };

            Application.Run(form);
            return Installer.Failed ? 1 : 0;
        }

        /// <summary>开始安装：路径归一化 → 可写探测（不可写就问是否提权）→ 后台解压</summary>
        private static void BeginInstall(SetupForm form, bool normalize = true, bool allowElevate = true)
        {
            if (form.Cur != SetupForm.State.Main && form.Cur != SetupForm.State.Failed) return;

            string target = form.GetPath();
            if (normalize) target = Installer.NormalizeTarget(target);
            if (target.Length == 0) { form.SetPathError("请先选择安装位置"); return; }
            if (!string.Equals(target, form.GetPath(), StringComparison.OrdinalIgnoreCase))
                form.SetPath(target);

            string err;
            bool denied;
            if (!Installer.ProbeWritable(target, out err, out denied))
            {
                if (denied && allowElevate)
                {
                    string body = "这个位置需要管理员权限才能写入：\n" + target +
                                  "\n\n是否以管理员身份重新启动安装程序？安装会继续，聊天记录不受影响。";
                    using (PromptForm p = new PromptForm("// ADMIN", "需要管理员权限",
                        body, new string[] { "以管理员身份重启", "取消" }))
                    {
                        p.PlayEntrance();
                        p.ShowDialog(form);
                        if (p.Selected == 0)
                        {
                            if (Installer.RelaunchElevated(target, form.OptDesktop, form.OptStartMenu, true))
                                form.Close();
                            else
                                form.SetPathError("已取消管理员授权，安装没有开始。");
                        }
                        return;
                    }
                }
                form.SetPathError(err);
                return;
            }

            form.SetPathError("");
            form.ToInstalling();

            bool desktop = form.OptDesktop, startMenu = form.OptStartMenu;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    Installer.Install(target, desktop, startMenu,
                        delegate(long d, long t) { ToUi(form, delegate { form.ReportBytes(d, t); }); },
                        delegate(int n, int t2, string name) { ToUi(form, delegate { form.ReportEntry(n, t2, name); }); });
                    ToUi(form, delegate { form.ToDone(); });
                    if (form.OptRun)
                    {
                        try
                        {
                            string exe = Path.Combine(target, AppInfo.AppSubDir, AppInfo.AppExe);
                            Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = Path.GetDirectoryName(exe) });
                        }
                        catch (Exception ex) { Installer.Log("auto run FAIL " + ex.Message); }
                    }
                }
                catch (Exception ex)
                {
                    Installer.Failed = true;
                    string msg = ex.Message;
                    ToUi(form, delegate { form.ToFailed(msg); });
                }
            });
        }

        private static void ToUi(Form form, Action a)
        {
            try
            {
                if (form.IsHandleCreated && !form.IsDisposed) form.BeginInvoke(a);
            }
            catch { }
        }

        private static bool HasFlag(string[] args, string flag)
        {
            foreach (string a in args)
                if (string.Equals(a.Trim().Trim('"'), flag, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static string ArgValue(string[] args, string prefix)
        {
            foreach (string a in args)
            {
                // "/D=C:\带 空格\目录" 这种整体带引号的写法也要认
                string t = a.Trim().Trim('"');
                if (t.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    return t.Substring(prefix.Length).Trim('"');
            }
            return "";
        }
    }
}
