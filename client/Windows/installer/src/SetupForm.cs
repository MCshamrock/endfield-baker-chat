// SetupForm.cs —— 安装器界面（四态：主界面 / 安装中 / 安装完成 / 安装失败）
//
// 版面：左栏主视觉 + 字标；右栏表单，安装中/完成/失败时右栏盖一块白板（看板）。
// 所有坐标是逻辑像素（940×620），绘制由 EfForm 按 DPI 缩放。

using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using BakerUi;

namespace BakerSetup
{
    internal class SetupForm : EfForm
    {
        public enum State { Main, Installing, Done, Failed }

        public State Cur = State.Main;

        // ---- 由 Installer 写入的真实进度 ----
        public string TargetDir = "";
        public bool OptDesktop = true;
        public bool OptStartMenu = true;
        public bool OptRun = false;
        public long PhaseABytes, PhaseATotal;
        public int EntriesDone, EntriesTotal;
        public string CurrentEntry = "";
        public string ErrorText = "";
        public readonly Tween Bar = new Tween(0f);

        public event EventHandler StartRequested;
        public event EventHandler RunRequested;
        public event EventHandler ReadmeRequested;
        public event EventHandler CloseRequested;

        // ---- 版面常量 ----
        private const float Fw = 940f, Fh = 620f;
        private const float LX = 28f;
        private const float LW = 284f;
        private const float ArtSize = 248f;
        private const float ArtY = 44f;
        private const float RX = 340f;
        private const float RW = 572f;
        private const float ColR = RX + RW;

        private readonly Tween _strip = new Tween(0f);
        private readonly Tween _cover = new Tween(0f);
        private readonly Tween _shutter = new Tween(1f);

        private Bitmap _art;
        private TextBox _path;
        private EfButton _browse, _cancel, _start, _closeX, _run, _readme, _close, _retry;
        private EfChip _cDesktop, _cMenu, _cRun;
        private string _pathError = "";

        public SetupForm()
        {
            Text = "Baker AI 4 " + Facts.Version + " 安装程序";
            Anims.Add(_strip);
            Anims.Add(_cover);
            Anims.Add(_shutter);
            Anims.Add(Bar);

            try
            {
                using (Stream st = Assembly.GetExecutingAssembly().GetManifestResourceStream("CoverArt.png"))
                    if (st != null) _art = new Bitmap(Image.FromStream(st));
            }
            catch { _art = null; }

            _closeX = MakeButton(EfButton.Kind.Ghost, "", 15f);
            _closeX.AccessibleName = "关闭安装程序";
            _closeX.Activated += delegate { Raise(CloseRequested); };

            _path = new TextBox();
            _path.BorderStyle = BorderStyle.None;
            _path.BackColor = Theme.Panel;
            _path.ForeColor = Theme.Ink;
            _path.AccessibleName = "安装位置";
            _path.TextChanged += delegate { Invalidate(); };
            _path.GotFocus += delegate { Invalidate(); };
            _path.LostFocus += delegate { Invalidate(); };
            Controls.Add(_path);

            _browse = MakeButton(EfButton.Kind.Secondary, "浏览", 13f);
            _browse.AccessibleName = "浏览安装位置";
            _browse.Activated += OnBrowse;

            _cDesktop = MakeChip("桌面快捷方式");
            _cMenu = MakeChip("开始菜单");
            _cRun = MakeChip("安装后启动");
            _cDesktop.Checked = OptDesktop;
            _cMenu.Checked = OptStartMenu;
            _cRun.Checked = OptRun;
            _cDesktop.AccessibleName = "创建桌面快捷方式";
            _cMenu.AccessibleName = "创建开始菜单快捷方式";
            _cRun.AccessibleName = "安装完成后立即启动";

            _cDesktop.Activated += delegate { OptDesktop = !OptDesktop; _cDesktop.Checked = OptDesktop; _cDesktop.Invalidate(); };
            _cMenu.Activated += delegate { OptStartMenu = !OptStartMenu; _cMenu.Checked = OptStartMenu; _cMenu.Invalidate(); };
            _cRun.Activated += delegate { OptRun = !OptRun; _cRun.Checked = OptRun; _cRun.Invalidate(); };

            _cancel = MakeButton(EfButton.Kind.Secondary, "取消", 15f);
            _cancel.AccessibleName = "取消安装";
            _cancel.Activated += delegate { Raise(CloseRequested); };

            _start = MakeButton(EfButton.Kind.Primary, "开始安装", 16f);
            _start.AccessibleName = "开始安装";
            _start.Activated += delegate { Raise(StartRequested); };

            _run = MakeButton(EfButton.Kind.Primary, "立即运行", 16f);
            _run.AccessibleName = "立即运行 Baker AI 4";
            _run.Activated += delegate { Raise(RunRequested); };

            _readme = MakeButton(EfButton.Kind.Secondary, "使用说明", 15f);
            _readme.AccessibleName = "打开使用说明";
            _readme.Activated += delegate { Raise(ReadmeRequested); };

            _close = MakeButton(EfButton.Kind.Secondary, "关闭", 15f);
            _close.AccessibleName = "关闭安装程序";
            _close.Activated += delegate { Raise(CloseRequested); };

            _retry = MakeButton(EfButton.Kind.Primary, "重新安装", 16f);
            _retry.AccessibleName = "重新安装";
            _retry.Activated += delegate { Raise(StartRequested); };

            // 键盘顺序：位置 -> 浏览 -> 三个选项 -> 取消 -> 开始
            _path.TabIndex = 0;
            _browse.TabIndex = 1;
            _cDesktop.TabIndex = 2;
            _cMenu.TabIndex = 3;
            _cRun.TabIndex = 4;
            _cancel.TabIndex = 5;
            _start.TabIndex = 6;
            _closeX.TabIndex = 9;

            ApplyState();
        }

        private void Raise(EventHandler h)
        {
            if (h != null) h(this, EventArgs.Empty);
        }

        private EfButton MakeButton(EfButton.Kind kind, string text, float zhPx)
        {
            EfButton b = new EfButton();
            b.Host = this;
            b.Style = kind;
            b.Text2 = text;
            b.Cut = Theme.CutButton;
            b.Font = EfFont.Zh(zhPx, true);
            b.Visible = false;
            Controls.Add(b);
            return b;
        }

        private EfChip MakeChip(string text)
        {
            EfChip c = new EfChip();
            c.Host = this;
            c.Text2 = text;
            c.Font = EfFont.Zh(13f, false);
            c.Visible = false;
            Controls.Add(c);
            return c;
        }

        // ------------------------------------------------------------ 布局

        protected override void OnLaidOut()
        {
            SetLogicalSize(Fw, Fh);

            Rectangle pr = PB(352f, 155f, 412f, 24f);
            _path.SetBounds(pr.X, pr.Y, pr.Width, pr.Height);
            _path.Font = EfFont.Zh(13f * S, false);

            _browse.Place(792f, 144f, 120f, 44f, S);

            float x = RX;
            x = PlaceChip(_cDesktop, x, 204f);
            x = PlaceChip(_cMenu, x + 10f, 204f);
            PlaceChip(_cRun, x + 10f, 204f);

            _cancel.Place(592f, 520f, 104f, 52f, S);
            _start.Place(708f, 520f, 204f, 52f, S);

            _run.Place(516f, 520f, 152f, 52f, S);
            _readme.Place(680f, 520f, 124f, 52f, S);

            _retry.Place(676f, 520f, 116f, 52f, S);

            ApplyState();
            if (Cur == State.Main && _path != null)
            {
                ActiveControl = _path;
                _path.SelectionStart = 0;
                _path.SelectionLength = 0;
            }
        }

        private float PlaceChip(EfChip c, float x, float y)
        {
            float w;
            using (Bitmap bmp = new Bitmap(1, 1))
            using (Graphics g = Graphics.FromImage(bmp))
            {
                g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
                w = Dx.Measure(g, c.Text2, EfFont.Zh(13f, true));
            }
            w += 12f + 10f + Theme.CutSmall;
            c.Place(x, y, w, 32f, S);
            return x + w;
        }

        public void ApplyState()
        {
            bool main = Cur == State.Main;
            if (_path == null) return;

            _path.Visible = main;
            _browse.Visible = main;
            _cDesktop.Visible = main;
            _cMenu.Visible = main;
            _cRun.Visible = main;
            _cancel.Visible = main;
            _start.Visible = main;

            _run.Visible = Cur == State.Done;
            _readme.Visible = Cur == State.Done;

            _closeX.Visible = Cur != State.Installing;
            _closeX.Place(Cur == State.Failed ? 884f : 894f, 12f, 30f, 30f, S);

            _close.Visible = Cur == State.Done || Cur == State.Failed;
            _close.Place(816f, 520f, 96f, 52f, S);

            _retry.Visible = Cur == State.Failed;
            Invalidate(true);
        }

        // ------------------------------------------------------------ 状态切换

        public void ToInstalling()
        {
            Cur = State.Installing;
            ApplyState();
            _shutter.To(0f, 0f, false);
            _shutter.To(1f, 360f, !ReducedMotion);
            Bar.To(0f, 0f, false);
        }

        public void ToDone()
        {
            Cur = State.Done;
            ApplyState();
            _shutter.To(0f, 0f, false);
            _shutter.To(1f, 360f, !ReducedMotion);
            if (_run.Visible) _run.Focus();
        }

        public void ToFailed(string err)
        {
            Cur = State.Failed;
            ErrorText = err ?? "";
            ApplyState();
            _shutter.To(0f, 0f, false);
            _shutter.To(1f, 360f, !ReducedMotion);
            if (_retry.Visible) _retry.Focus();
        }

        public void ReportBytes(long done, long total)
        {
            PhaseABytes = done;
            PhaseATotal = total;
            float p = total > 0 ? (float)((double)done / total) : 0f;
            Bar.To(p * 0.2f, 0f, false);
            Invalidate(true);
        }

        public void ReportEntry(int done, int total, string name)
        {
            EntriesDone = done;
            EntriesTotal = total;
            CurrentEntry = name ?? "";
            float p = total > 0 ? (float)((double)done / total) : 0f;
            Bar.To(0.2f + p * 0.8f, 0f, false);
            Invalidate(true);
        }

        // ------------------------------------------------------------ 路径

        public void SetPath(string dir)
        {
            TargetDir = dir ?? "";
            if (_path != null && _path.Text != TargetDir) _path.Text = TargetDir;
            Invalidate(true);
        }

        public string GetPath()
        {
            if (_path != null) TargetDir = (_path.Text ?? "").Trim();
            return TargetDir;
        }

        public void SetPathError(string msg)
        {
            _pathError = msg ?? "";
            Invalidate(true);
        }

        private void OnBrowse(object sender, EventArgs e)
        {
            using (FolderBrowserDialog d = new FolderBrowserDialog())
            {
                d.Description = "选择安装位置";
                d.ShowNewFolderButton = true;
                string cur = GetPath();
                if (Directory.Exists(cur)) d.SelectedPath = cur;
                if (d.ShowDialog(this) == DialogResult.OK)
                {
                    // 选 D:\ → 装到 D:\Baker AI 4：目录名统一，数据/快捷方式都跟着这个文件夹
                    SetPath(Installer.NormalizeTarget(d.SelectedPath));
                    SetPathError("");
                    Invalidate(true);
                }
            }
        }

        // ------------------------------------------------------------ 键盘 / 关闭

        protected override void OnEscapeKey()
        {
            if (Cur != State.Installing) Raise(CloseRequested);
        }

        protected override void OnEnterKey()
        {
            if (Cur == State.Main) Raise(StartRequested);
            else if (Cur == State.Done) Raise(RunRequested);
            else if (Cur == State.Failed) Raise(StartRequested);
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (Cur == State.Installing) e.Cancel = true;
            base.OnFormClosing(e);
        }

        // ============================================================ 绘制

        protected override void PaintContent(Graphics g)
        {
            Dx.Fill(g, new RectangleF(0, 0, Fw, Fh), Theme.Paper);

            // 三色条：只在页面最顶端
            if (_strip.Value > 0.001f)
            {
                float w = Fw * _strip.Value;
                Dx.Fill(g, new RectangleF(0, 0, w / 3f, Theme.Strip), Theme.TriA);
                Dx.Fill(g, new RectangleF(w / 3f, 0, w / 3f, Theme.Strip), Theme.Signal);
                Dx.Fill(g, new RectangleF(w * 2f / 3f, 0, w / 3f, Theme.Strip), Theme.TriC);
            }

            DrawCover(g);
            DrawBrand(g);

            if (Cur == State.Main || Cur == State.Installing) DrawHead(g);

            if (Cur == State.Main) DrawMain(g);
            else DrawBoard(g);

            if (Cur == State.Installing) DrawInstalling(g);
            if (Cur == State.Done) DrawDone(g);
            if (Cur == State.Failed) DrawFailed(g);

            using (Pen p = new Pen(Theme.Ink, 1f))
            {
                p.Alignment = PenAlignment.Inset;
                g.DrawRectangle(p, 0, 0, Fw - 1f, Fh - 1f);
            }
        }

        // ---- 左栏：画框主视觉（切角画框 + 斜线纹影子） ----
        private void DrawCover(Graphics g)
        {
            RectangleF frame = new RectangleF(LX, ArtY, ArtSize, ArtSize);

            // 斜线纹影子：右下错 12px，和画框同形（左上 + 右下切角）
            GraphicsState stShadow = g.Save();
            using (GraphicsPath a = Dx.CutPath(new RectangleF(frame.X + 12f, frame.Y + 12f, frame.Width, frame.Height),
                Corner.BR, Theme.CutFrame))
            {
                g.SetClip(a, CombineMode.Intersect);
                using (GraphicsPath b = Dx.CutPath(new RectangleF(frame.X + 11f, frame.Y + 11f, frame.Width + 2f, frame.Height + 2f),
                    Corner.TL, Theme.CutFrame))
                {
                    g.SetClip(b, CombineMode.Intersect);
                    Dx.Hatch(g, new RectangleF(frame.X + 12f, frame.Y + 12f, frame.Width, frame.Height), Theme.HatchInk);
                }
            }
            g.Restore(stShadow);

            // 主视觉本体：从左往右拉开入场
            GraphicsState st = g.Save();
            using (GraphicsPath clip = Dx.CutPath(frame, Corner.TL, Theme.CutFrame))
            {
                g.SetClip(clip, CombineMode.Intersect);
                using (GraphicsPath clip2 = Dx.CutPath(frame, Corner.BR, Theme.CutFrame))
                {
                    g.SetClip(clip2, CombineMode.Intersect);
                    if (_art != null)
                    {
                        RectangleF vis = new RectangleF(frame.X, frame.Y,
                            frame.Width * Math.Max(0.001f, _cover.Value), frame.Height);
                        g.SetClip(vis, CombineMode.Intersect);
                        g.DrawImage(_art, frame);
                    }
                    else
                    {
                        Dx.Fill(g, frame, Theme.Chip);
                    }
                }
            }
            g.Restore(st);

            Dx.FrameCut(g, frame, Theme.CutFrame, Theme.Ink);
        }

        // ---- 左栏：字标 + 字段 + 三条事实 ----
        private void DrawBrand(Graphics g)
        {
            Dx.Eyebrow(g, "// BAKER AI 4", EfFont.Label(11f), Theme.Ink2, LX, 322f);

            Font disp = EfFont.Display(44f);
            Dx.Txt(g, "BAKER", disp, Theme.Ink, LX, 338f);
            float w = Dx.Measure(g, "BAKER", disp);
            Dx.Hollow(g, "AI 4", disp, Theme.Ink, LX + w + 10f, 338f, 1.5f);

            Dx.Fields(g, new string[] { "v" + Facts.Version, "Windows x64" }, EfFont.Field(12f), Theme.Ink2, LX, 396f);
            Dx.HLine(g, LX, LX + LW, 420f, Theme.Rule);

            DrawFact(g, "No.01", "聊天记录存安装目录", 434f);
            DrawFact(g, "No.02", "与旧版并存不覆盖", 468f);
            DrawFact(g, "No.03", "卸载时可保留数据", 502f);

            Dx.Hatch(g, new RectangleF(LX, 546f, LW, 10f), Theme.HatchInk);
        }

        private void DrawFact(Graphics g, string no, string text, float y)
        {
            Dx.Txt(g, no, EfFont.Field(12f), Theme.Ink2, LX, y);
            Dx.Txt(g, text, EfFont.Zh(12.5f, false), Theme.Ink, LX + 56f, y - 1f);
        }

        // ---- 右栏表头 ----
        private void DrawHead(Graphics g)
        {
            Dx.Eyebrow(g, "// INSTALLER", EfFont.Label(11f), Theme.Ink2, RX, 44f);
            Dx.Txt(g, "安装向导", EfFont.Zh(27f, true), Theme.Ink, RX, 56f);

            string tag = "v" + Facts.Version;
            Font tf = EfFont.Field(12f);
            float tw = Dx.Measure(g, tag, tf) + 20f;
            Dx.FillCut(g, new RectangleF(ColR - tw, 60f, tw, 24f), Corner.BR, Theme.CutSmall, Theme.TagBg);
            Dx.Txt(g, tag, tf, Theme.TagFg, ColR - tw + 10f, 60f + (24f - tf.GetHeight(g)) / 2f);

            Dx.HLine(g, RX, ColR, 104f, Theme.Rule);
        }

        // ---- 主界面 ----
        private void DrawMain(Graphics g)
        {
            Dx.Txt(g, "安装位置", EfFont.Zh(12.5f, true), Theme.Ink2, RX, 121f);

            RectangleF box = new RectangleF(RX, 144f, 440f, 44f);
            Dx.Fill(g, box, Theme.Panel);
            bool focus = _path != null && _path.Focused;
            using (Pen p = new Pen(focus ? Theme.Ink : Theme.Rule, focus ? 2f : 1f))
            {
                p.Alignment = PenAlignment.Inset;
                g.DrawRectangle(p, box.X, box.Y, box.Width, box.Height);
            }

            // 「本次更新」看板（条目来自 Notes.cs，发版只改那个文件）
            RectangleF card = new RectangleF(RX, 252f, RW, 168f);
            Dx.Fill(g, card, Theme.Panel);
            Dx.Box(g, card, Theme.Rule);
            Dx.Fill(g, new RectangleF(card.X, card.Y, 72f, 5f), Theme.Ink);

            Font titleF = EfFont.Zh(16f, true);
            Dx.Txt(g, Notes.Title, titleF, Theme.Ink, RX + 16f, 266f);
            float titleW = Dx.Measure(g, Notes.Title, titleF);
            float bandX = RX + 16f + titleW + 12f;
            Dx.Fill(g, new RectangleF(bandX, 274f, 6f, 10f), Theme.Ink);
            Dx.Hatch(g, new RectangleF(bandX + 10f, 274f, ColR - 16f - (bandX + 10f), 10f), Theme.HatchInk);

            string[][] rows = Notes.Rows;
            for (int i = 0; i < rows.Length; i++)
            {
                float y = 292f + i * 32f;
                Dx.Txt(g, rows[i][0], EfFont.Field(12f), Theme.Ink2, RX + 16f, y + 7f);
                Dx.Txt(g, rows[i][1], EfFont.Zh(12.5f, false), Theme.Ink, RX + 70f, y + 6f);
                Dx.TxtRight(g, rows[i][2], EfFont.Field(12f), Theme.Ink2, ColR - 16f, y + 7f);
                if (i < rows.Length - 1) Dx.HLine(g, RX + 16f, ColR - 16f, y + 29f, Theme.Rule);
            }

            Dx.Fields(g, new string[] {
                Facts.BuildDate, "v" + Facts.Version,
                Facts.Entries.ToString("N0") + " 个文件", "安装后占 " + Facts.InstalledSizeText },
                EfFont.Field(11.5f), Theme.Ink2, RX, 438f);

            if (_pathError.Length > 0)
            {
                Dx.Notice(g, "出错", _pathError, EfFont.Zh(12.5f, false), EfFont.Zh(11f, true),
                    RX, 462f, RW, 38f);
            }
            else
            {
                Dx.HLine(g, RX, ColR, 462f, Theme.Rule);
                Dx.Txt(g, "选中的目录下会自动新建 Baker AI 4 文件夹；需要管理员权限时会提示以管理员身份重启。",
                    EfFont.Zh(11.5f, false), Theme.Ink2, RX, 476f);
            }

            Dx.Txt(g, "Enter 开始安装   │   Esc 取消", EfFont.Field(10.5f), Theme.Ink2, RX, 588f);
        }

        // ---- 安装中 / 完成 / 失败：右栏白板 ----
        private void DrawBoard(Graphics g)
        {
            RectangleF b = new RectangleF(RX, 112f, RW, 392f);
            float h = b.Height * Math.Max(0f, Math.Min(1f, _shutter.Value));
            if (h <= 0.5f) return;

            GraphicsState st = g.Save();
            g.SetClip(new RectangleF(b.X, b.Y, b.Width, h), CombineMode.Intersect);
            Dx.Fill(g, b, Theme.Panel);
            Dx.Box(g, b, Theme.Ink);
            g.Restore(st);
        }

        private void DrawInstalling(Graphics g)
        {
            if (_shutter.Value < 0.55f) return;

            Dx.Eyebrow(g, "// EXTRACTING", EfFont.Label(11f), Theme.Ink2, RX + 28f, 138f);
            Dx.Txt(g, "正在安装", EfFont.Zh(22f, true), Theme.Ink, RX + 28f, 152f);

            int pct = (int)Math.Round(Bar.Value * 100.0);
            if (pct < 0) pct = 0;
            if (pct > 100) pct = 100;
            Dx.TxtRight(g, pct + "%", EfFont.Display(46f), Theme.Ink, ColR - 28f, 136f);

            float bx = RX + 28f, bw = RW - 56f, segGap = 3f;
            float segW = (bw - segGap * 23f) / 24f;
            int lit = (int)Math.Floor(Bar.Value * 24f + 0.0001f);
            for (int i = 0; i < 24; i++)
            {
                RectangleF r = new RectangleF(bx + i * (segW + segGap), 214f, segW, 16f);
                Dx.Fill(g, r, i < lit ? Theme.Signal : Theme.Chip);
            }

            if (EntriesTotal <= 0 && PhaseATotal > 0)
                Dx.Txt(g, "正在写入临时文件 " + (PhaseABytes / 1048576).ToString("N0") + " / "
                    + (PhaseATotal / 1048576).ToString("N0") + " MB",
                    EfFont.Zh(12.5f, false), Theme.Ink2, bx, 250f);
            else
                Dx.Txt(g, "正在解压 " + EntriesDone.ToString("N0") + " / " + Math.Max(EntriesTotal, 0).ToString("N0") + " 个文件",
                    EfFont.Zh(12.5f, false), Theme.Ink2, bx, 250f);

            if (!string.IsNullOrEmpty(CurrentEntry))
                Dx.Txt(g, Ellip(g, CurrentEntry, EfFont.Zh(11f, false), bw), EfFont.Zh(11f, false), Theme.Ink2, bx, 274f);

            DrawSteps(g, 1);

            Dx.Txt(g, "目标目录", EfFont.Zh(12f, true), Theme.Ink2, bx, 356f);
            Dx.Txt(g, Ellip(g, TargetDir, EfFont.Zh(12f, false), bw), EfFont.Zh(12f, false), Theme.Ink, bx, 378f);
            Dx.Txt(g, "安装期间请不要关闭窗口。", EfFont.Zh(11.5f, false), Theme.Ink2, bx, 406f);
        }

        private void DrawDone(Graphics g)
        {
            if (_shutter.Value < 0.55f) return;

            Dx.Eyebrow(g, "// COMPLETE", EfFont.Label(11f), Theme.Ink2, RX + 28f, 138f);
            Dx.Txt(g, "安装完成", EfFont.Zh(22f, true), Theme.Ink, RX + 28f, 152f);

            RectangleF ck = new RectangleF(ColR - 76f, 136f, 48f, 48f);
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

            Dx.Txt(g, "安装位置", EfFont.Zh(12f, true), Theme.Ink2, RX + 28f, 210f);
            Dx.Txt(g, Ellip(g, TargetDir, EfFont.Zh(13f, false), RW - 56f), EfFont.Zh(13f, false), Theme.Ink,
                RX + 28f, 232f);

            Dx.Notice(g, "提示", "聊天记录保存在安装目录；卸载时可选择是否保留数据。",
                EfFont.Zh(12.5f, false), EfFont.Zh(11f, true), RX + 28f, 264f, RW - 56f, 46f);

            Dx.Fields(g, new string[] {
                "安装后 " + Facts.InstalledSizeText,
                OptDesktop ? "桌面快捷方式" : "无桌面快捷方式",
                OptRun ? "已自动启动" : "未自动启动" },
                EfFont.Field(11.5f), Theme.Ink2, RX + 28f, 328f);

            Dx.Txt(g, "数据目录", EfFont.Zh(12f, true), Theme.Ink2, RX + 28f, 366f);
            Dx.Txt(g, Ellip(g, Path.Combine(TargetDir, "bakerai_data"), EfFont.Zh(12f, false), RW - 56f),
                EfFont.Zh(12f, false), Theme.Ink, RX + 28f, 386f);

            Dx.Txt(g, "卸载：设置 → 应用 → Baker AI 4，或运行安装目录下的「" + AppInfo.UninstallExe + "」。",
                EfFont.Zh(11.5f, false), Theme.Ink2, RX + 28f, 424f);

            Dx.Txt(g, "Enter 立即运行   │   Esc 关闭", EfFont.Field(10.5f), Theme.Ink2, RX + 28f, 588f);
        }

        private void DrawFailed(Graphics g)
        {
            if (_shutter.Value < 0.55f) return;

            Dx.Eyebrow(g, "// FAILED", EfFont.Label(11f), Theme.Ink2, RX + 28f, 138f);
            Dx.Txt(g, "安装失败", EfFont.Zh(22f, true), Theme.Ink, RX + 28f, 152f);

            RectangleF mark = new RectangleF(ColR - 76f, 136f, 48f, 48f);
            GraphicsState st = g.Save();
            using (GraphicsPath clip = Dx.CutPath(mark, Corner.BR, 8f))
            {
                g.SetClip(clip, CombineMode.Intersect);
                Dx.Hatch(g, mark, Theme.HatchInk);
            }
            g.Restore(st);
            using (Pen p = new Pen(Theme.Ink, 3f))
                g.DrawLine(p, mark.X + 24f, mark.Y + 12f, mark.X + 24f, mark.Y + 30f);
            Dx.Fill(g, new RectangleF(mark.X + 22f, mark.Y + 35f, 4f, 4f), Theme.Ink);

            string err = string.IsNullOrEmpty(ErrorText) ? "安装过程出错，请重试。" : ErrorText;
            Dx.Notice(g, "出错", err, EfFont.Zh(12.5f, false), EfFont.Zh(11f, true),
                RX + 28f, 208f, RW - 56f, 84f);

            Dx.Txt(g, "目标目录", EfFont.Zh(12f, true), Theme.Ink2, RX + 28f, 312f);
            Dx.Txt(g, Ellip(g, TargetDir, EfFont.Zh(12f, false), RW - 56f), EfFont.Zh(12f, false),
                Theme.Ink, RX + 28f, 332f);

            Dx.Txt(g, "日志文件", EfFont.Zh(12f, true), Theme.Ink2, RX + 28f, 372f);
            Dx.Txt(g, Installer.LogPath, EfFont.Field(11.5f), Theme.Ink, RX + 28f, 392f);

            Dx.Txt(g, "装到一半失败时，已经解出来的文件会留在目标目录，重试会直接覆盖；换目录重装也可以。",
                EfFont.Zh(11.5f, false), Theme.Ink2, RX + 28f, 434f);

            Dx.Txt(g, "Esc 关闭", EfFont.Field(10.5f), Theme.Ink2, RX + 28f, 588f);
        }

        /// <summary>步骤条：01 选择位置 / 02 解压文件 / 03 完成设置</summary>
        private void DrawSteps(Graphics g, int current)
        {
            string[] nos = new string[] { "01", "02", "03" };
            string[] names = new string[] { "选择位置", "解压文件", "完成设置" };
            float w = 160f, h = 32f, pitch = 172f, y = 300f;
            float x0 = RX + 28f;
            for (int i = 0; i < 3; i++)
            {
                float x = x0 + i * pitch;
                RectangleF r = new RectangleF(x, y, w, h);
                int s = i < current ? 2 : (i == current ? 1 : 0);
                if (s == 2) Dx.Fill(g, r, Theme.Ink);
                else if (s == 1) Dx.Fill(g, r, Theme.Signal);
                else Dx.Fill(g, r, Theme.Chip);

                Color cDone = Theme.Dark ? Theme.Paper : Color.White;
                Dx.Txt(g, nos[i], EfFont.Field(12f), s == 2 ? cDone : (s == 1 ? Theme.OnSignal : Theme.Ink2),
                    x + 10f, y + 9f);
                Dx.Txt(g, names[i], EfFont.Zh(12.5f, true),
                    s == 2 ? cDone : (s == 1 ? Theme.OnSignal : Theme.Ink2), x + 40f, y + 8f);
                if (i < 2) Dx.HLine(g, x + w + 1f, x + pitch - 1f, y + h / 2f, Theme.Rule);
            }
        }

        // ---- 文字工具 ----
        private static string Ellip(Graphics g, string s, Font f, float maxW)
        {
            if (string.IsNullOrEmpty(s)) return s;
            if (Dx.Measure(g, s, f) <= maxW) return s;
            string cut = s;
            while (cut.Length > 1 && Dx.Measure(g, cut + "…", f) > maxW) cut = cut.Substring(0, cut.Length - 1);
            return cut + "…";
        }

        /// <summary>入场编排：三色条扫出 + 主视觉拉开（整段 ≤ 650ms）</summary>
        public void PlayEntrance()
        {
            if (ReducedMotion)
            {
                _strip.To(1f, 0f, false);
                _cover.To(1f, 0f, false);
                Invalidate(true);
                return;
            }
            _strip.To(0f, 0f, false);
            _cover.To(0f, 0f, false);
            _strip.To(1f, 360f, true);
            _cover.To(1f, 420f, true);
            Invalidate(true);
        }
    }
}
