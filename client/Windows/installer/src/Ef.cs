// Ef.cs —— 安装器界面的底座：色板、字体、绘制、按钮/筛选块/方框、无边框窗口
//
// 这层只做"画"，不含任何安装逻辑。所有尺寸都是逻辑像素（96 DPI 下的 px），
// 运行时按窗口 DPI 缩放：绘制用 g.ScaleTransform(S,S)，子控件边界手动乘 S。
//
// 约定：
//   唯一强调色（默认黄）只做填充，表示"当前 / 可点 / 要注意"；
//   切掉一个角 = 可以按，不可点的容器全是直角；
//   圆角为 0，不用投影、发光、渐变；红色只给删除；
//   细分隔线 + 真实数字字段 + 45° 斜线纹。

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace BakerUi
{
    /// <summary>色板与常量（浅色为默认，深色是另一套完整主题）</summary>
    internal static class Theme
    {
        public const float Strip = 3f;      // 三色条
        public const float CutSmall = 7f;   // 标签、筛选块
        public const float CutButton = 8f;  // 按钮
        public const float CutFrame = 34f;  // 主视觉画框

        public static Color Paper;
        public static Color Panel;
        public static Color Ink;
        public static Color Ink2;
        public static Color Rule;
        public static Color Chip;
        public static Color Signal;
        public static Color OnSignal;
        public static Color TagBg;
        public static Color TagFg;
        public static Color Danger;
        public static Color TriA;
        public static Color TriC;

        public static bool Dark;

        static Theme()
        {
            SetLight();
        }

        public static void SetLight()
        {
            Dark = false;
            Paper = Hex("F4F4F2");
            Panel = Hex("FFFFFF");
            Ink = Hex("161616");
            Ink2 = Hex("5C5C5C");
            Rule = Hex("D9D9D6");
            Chip = Hex("E4E4E0");
            Signal = Hex("FFFA00");
            OnSignal = Hex("161616");
            TagBg = Hex("161616");
            TagFg = Hex("FFFFFF");
            Danger = Hex("B3261E");
            TriA = Hex("FF00F0");
            TriC = Hex("00FFA2");
        }

        public static void SetDark()
        {
            Dark = true;
            Paper = Hex("191919");
            Panel = Hex("222222");
            Ink = Hex("EDEDED");
            Ink2 = Hex("A3A3A3");
            Rule = Hex("353535");
            Chip = Hex("2E2E2E");
            Signal = Hex("FFFA00");
            OnSignal = Hex("161616");
            TagBg = Hex("3A3A3A");
            TagFg = Hex("EDEDED");
            Danger = Hex("FF8A7A");
            TriA = Hex("FF00F0");
            TriC = Hex("00FFA2");
        }

        public static Color Hex(string s)
        {
            return Color.FromArgb(
                Convert.ToInt32(s.Substring(0, 2), 16),
                Convert.ToInt32(s.Substring(2, 2), 16),
                Convert.ToInt32(s.Substring(4, 2), 16));
        }

        /// <summary>斜线纹的墨色（22%）</summary>
        public static Color HatchInk
        {
            get { return Color.FromArgb(56, Ink); }
        }
    }

    internal enum Corner
    {
        None,
        TL,
        TR,
        BR,
        BL
    }

    /// <summary>线性补间：负责所有动效的数值</summary>
    internal class Tween
    {
        private float _from, _to, _value;
        private double _elapsed, _dur;
        private bool _active;

        public Tween(float initial)
        {
            _from = _to = _value = initial;
        }

        public float Value { get { return _value; } }
        public bool Active { get { return _active; } }

        public void To(float target, float ms, bool animate)
        {
            if (!animate || ms <= 0f || Math.Abs(target - _value) < 0.0005f)
            {
                _from = _to = _value = target;
                _active = false;
                return;
            }
            _from = _value;
            _to = target;
            _elapsed = 0;
            _dur = ms;
            _active = true;
        }

        /// <summary>每帧推进；返回是否还在动</summary>
        public bool Advance(double dtMs)
        {
            if (!_active) return false;
            _elapsed += dtMs;
            float p = _dur <= 0 ? 1f : (float)(_elapsed / _dur);
            if (p >= 1f)
            {
                _value = _to;
                _active = false;
                return false;
            }
            _value = _from + (_to - _from) * Dx.Mech(p);
            return true;
        }
    }

    /// <summary>绘制工具：所有坐标都是逻辑像素</summary>
    internal static class Dx
    {
        // 三条曲线（tokens：snap 0.16,1,0.3,1 / mech 0.7,0,0.2,1 / ease 0.2,0.7,0.1,1）
        public static float Snap(float t) { return Bezier(t, 0.16f, 1f, 0.3f, 1f); }
        public static float Mech(float t) { return Bezier(t, 0.7f, 0f, 0.2f, 1f); }
        public static float Ease(float t) { return Bezier(t, 0.2f, 0.7f, 0.1f, 1f); }

        private static float Bezier(float t, float x1, float y1, float x2, float y2)
        {
            float lo = 0f, hi = 1f, u = t;
            for (int i = 0; i < 18; i++)
            {
                float x = Cubic(u, 0f, x1, x2, 1f);
                if (x < t) lo = u; else hi = u;
                u = (lo + hi) * 0.5f;
            }
            return Cubic(u, 0f, y1, y2, 1f);
        }

        private static float Cubic(float u, float p0, float p1, float p2, float p3)
        {
            float v = 1f - u;
            return v * v * v * p0 + 3f * v * v * u * p1 + 3f * v * u * u * p2 + u * u * u * p3;
        }

        // ---------------- 基础图元 ----------------

        public static void Fill(Graphics g, RectangleF r, Color c)
        {
            if (c.A == 0) return;
            using (SolidBrush b = new SolidBrush(c)) g.FillRectangle(b, r);
        }

        public static GraphicsPath CutPath(RectangleF r, Corner corner, float cut)
        {
            GraphicsPath p = new GraphicsPath();
            float k = Math.Max(0f, cut);
            if (k <= 0.01f || corner == Corner.None)
            {
                p.AddRectangle(r);
                return p;
            }
            switch (corner)
            {
                case Corner.TL:
                    p.AddPolygon(new PointF[] {
                        new PointF(r.Left + k, r.Top), new PointF(r.Right, r.Top),
                        new PointF(r.Right, r.Bottom), new PointF(r.Left, r.Bottom),
                        new PointF(r.Left, r.Top + k) });
                    break;
                case Corner.TR:
                    p.AddPolygon(new PointF[] {
                        new PointF(r.Left, r.Top), new PointF(r.Right - k, r.Top),
                        new PointF(r.Right, r.Top + k), new PointF(r.Right, r.Bottom),
                        new PointF(r.Left, r.Bottom) });
                    break;
                case Corner.BL:
                    p.AddPolygon(new PointF[] {
                        new PointF(r.Left, r.Top), new PointF(r.Right, r.Top),
                        new PointF(r.Right, r.Bottom), new PointF(r.Left + k, r.Bottom),
                        new PointF(r.Left, r.Bottom - k) });
                    break;
                default: // BR
                    p.AddPolygon(new PointF[] {
                        new PointF(r.Left, r.Top), new PointF(r.Right, r.Top),
                        new PointF(r.Right, r.Bottom - k), new PointF(r.Right - k, r.Bottom),
                        new PointF(r.Left, r.Bottom) });
                    break;
            }
            p.CloseFigure();
            return p;
        }

        public static void FillCut(Graphics g, RectangleF r, Corner corner, float cut, Color c)
        {
            using (GraphicsPath p = CutPath(r, corner, cut))
            using (SolidBrush b = new SolidBrush(c))
                g.FillPath(b, p);
        }

        /// <summary>主视觉画框：左上与右下各切一角，1px 墨边</summary>
        public static void FrameCut(Graphics g, RectangleF r, float cut, Color stroke)
        {
            using (GraphicsPath p = new GraphicsPath())
            {
                p.AddPolygon(new PointF[] {
                    new PointF(r.Left + cut, r.Top), new PointF(r.Right, r.Top),
                    new PointF(r.Right, r.Bottom - cut), new PointF(r.Right - cut, r.Bottom),
                    new PointF(r.Left, r.Bottom), new PointF(r.Left, r.Top + cut) });
                p.CloseFigure();
                using (Pen pen = new Pen(stroke, 1f))
                {
                    pen.Alignment = PenAlignment.Inset;
                    g.DrawPath(pen, p);
                }
            }
        }

        // ---------------- 线 ----------------

        public static void HLine(Graphics g, float x1, float x2, float y, Color c)
        {
            using (Pen p = new Pen(c, 1f))
                g.DrawLine(p, x1, y + 0.5f, x2, y + 0.5f);
        }

        public static void VLine(Graphics g, float x, float y1, float y2, Color c)
        {
            using (Pen p = new Pen(c, 1f))
                g.DrawLine(p, x + 0.5f, y1, x + 0.5f, y2);
        }

        public static void Box(Graphics g, RectangleF r, Color c)
        {
            HLine(g, r.Left, r.Right - 1f, r.Top, c);
            HLine(g, r.Left, r.Right - 1f, r.Bottom - 1f, c);
            VLine(g, r.Left, r.Top, r.Bottom - 1f, c);
            VLine(g, r.Right - 1f, r.Top, r.Bottom - 1f, c);
        }

        // ---------------- 斜线纹 ----------------

        /// <summary>45° 斜线纹（向右上），裁在 r 内</summary>
        public static void Hatch(Graphics g, RectangleF r, Color ink)
        {
            if (r.Width <= 0 || r.Height <= 0) return;
            GraphicsState st = g.Save();
            using (Region clip = new Region(r))
            {
                g.SetClip(clip, CombineMode.Intersect);
                using (Pen p = new Pen(ink, 1.5f))
                {
                    p.StartCap = LineCap.Flat;
                    p.EndCap = LineCap.Flat;
                    for (float x = r.Left - r.Height; x < r.Right + r.Height; x += 9.2f)
                        g.DrawLine(p, x, r.Bottom, x + r.Height, r.Top);
                }
            }
            g.Restore(st);
        }

        // ---------------- 字 ----------------

        public static void Txt(Graphics g, string text, Font f, Color c, float x, float y)
        {
            if (string.IsNullOrEmpty(text)) return;
            using (SolidBrush b = new SolidBrush(c))
            using (StringFormat sf = new StringFormat(StringFormat.GenericTypographic))
            {
                sf.FormatFlags = StringFormatFlags.NoWrap | StringFormatFlags.NoClip;
                g.DrawString(text, f, b, x, y, sf);
            }
        }

        public static void TxtRight(Graphics g, string text, Font f, Color c, float right, float y)
        {
            Txt(g, text, f, c, right - Measure(g, text, f), y);
        }

        public static float Measure(Graphics g, string text, Font f)
        {
            if (string.IsNullOrEmpty(text)) return 0f;
            using (StringFormat sf = new StringFormat(StringFormat.GenericTypographic))
            {
                sf.FormatFlags = StringFormatFlags.NoWrap | StringFormatFlags.MeasureTrailingSpaces;
                return g.MeasureString(text, f, 100000, sf).Width;
            }
        }

        /// <summary>按可用宽度逐字换行（中文没有单词边界，逐字判宽最稳）</summary>
        public static List<string> Wrap(Graphics g, string text, Font f, float maxW)
        {
            List<string> lines = new List<string>();
            if (string.IsNullOrEmpty(text)) { lines.Add(""); return lines; }

            string line = "";
            foreach (char ch in text)
            {
                string trial = line + ch;
                if (line.Length > 0 && Measure(g, trial, f) > maxW)
                {
                    lines.Add(line);
                    line = ch.ToString();
                }
                else
                {
                    line = trial;
                }
            }
            if (line.Length > 0) lines.Add(line);
            return lines;
        }

        /// <summary>宽体大写眉标，字距 0.14em</summary>
        public static void Eyebrow(Graphics g, string text, Font f, Color c, float x, float y)
        {
            float gap = f.Size * 0.14f;
            float cx = x;
            foreach (char ch in text)
            {
                string s = ch.ToString();
                Txt(g, s, f, c, cx, y);
                cx += Measure(g, s, f) + gap;
            }
        }

        /// <summary>竖线分隔的字段行</summary>
        public static void Fields(Graphics g, string[] parts, Font f, Color c, float x, float y)
        {
            float cx = x;
            for (int i = 0; i < parts.Length; i++)
            {
                if (i > 0)
                {
                    Txt(g, "│", f, Color.FromArgb(120, c), cx + 8f, y);
                    cx += 8f + Measure(g, "│", f) + 8f;
                }
                Txt(g, parts[i], f, c, cx, y);
                cx += Measure(g, parts[i], f);
            }
        }

        /// <summary>空心字（只描边，不填充）</summary>
        public static void Hollow(Graphics g, string text, Font f, Color stroke, float x, float y, float penWidth)
        {
            using (GraphicsPath path = new GraphicsPath())
            {
                path.AddString(text, f.FontFamily, (int)FontStyle.Regular, f.Size,
                    new PointF(x, y), StringFormat.GenericTypographic);
                using (Pen p = new Pen(stroke, penWidth))
                {
                    p.LineJoin = LineJoin.Miter;
                    g.DrawPath(p, path);
                }
            }
        }

        public static void Check(Graphics g, RectangleF ck, Color c, float weight)
        {
            using (Pen p = new Pen(c, weight))
            {
                p.StartCap = LineCap.Flat;
                p.EndCap = LineCap.Flat;
                p.LineJoin = LineJoin.Miter;
                g.DrawLines(p, new PointF[] {
                    new PointF(ck.X + ck.Width * 0.06f, ck.Y + ck.Height * 0.52f),
                    new PointF(ck.X + ck.Width * 0.36f, ck.Y + ck.Height * 0.86f),
                    new PointF(ck.X + ck.Width * 0.94f, ck.Y + ck.Height * 0.16f) });
            }
        }

        /// <summary>黄旗提示：白底细框 + 前面一块黄标签</summary>
        public static void Notice(Graphics g, string flag, string text, Font zh, Font flagFont,
            float x, float y, float w, float h)
        {
            RectangleF box = new RectangleF(x, y, w, h);
            Fill(g, box, Theme.Panel);
            Box(g, box, Theme.Ink);

            float flagW = Measure(g, flag, flagFont) + 20f;
            RectangleF tag = new RectangleF(x + 12f, y + 12f, flagW, 20f);
            FillCut(g, tag, Corner.BR, 5f, Theme.Signal);
            Txt(g, flag, flagFont, Theme.OnSignal, tag.X + 10f, tag.Y + (tag.Height - flagFont.GetHeight(g)) / 2f);

            RectangleF tr = new RectangleF(x + 12f + flagW + 12f, y + 11f, w - flagW - 50f, h - 20f);
            using (SolidBrush b = new SolidBrush(Theme.Ink))
            using (StringFormat sf = new StringFormat())
            {
                sf.FormatFlags = StringFormatFlags.NoClip;
                g.DrawString(text, zh, b, tr, sf);
            }
        }
    }

    /// <summary>字体：Saira 三个静态实例（拉丁）+ 系统中文字体；按尺寸缓存
    /// 私有字体集合必须用 FontFamily 对象建 Font：按名字查会落到系统字体</summary>
    internal static class EfFont
    {
        private static PrivateFontCollection _pfc;
        private static readonly List<IntPtr> _pinned = new List<IntPtr>();
        private static readonly Dictionary<string, Font> _cache = new Dictionary<string, Font>();
        private static FontFamily _field = FontFamily.GenericSansSerif;
        private static FontFamily _label = FontFamily.GenericSansSerif;
        private static FontFamily _display = FontFamily.GenericSansSerif;
        private static string _zh = "Microsoft YaHei UI";

        /// <summary>从自身资源里注册字体；失败则退回系统字体（不阻断安装）</summary>
        public static void Init()
        {
            try
            {
                _pfc = new PrivateFontCollection();
                AddResource("BakerSairaField.ttf");
                AddResource("BakerSairaLabel.ttf");
                AddResource("BakerSairaDisplay.ttf");
                foreach (FontFamily fam in _pfc.Families)
                {
                    string n = fam.Name;
                    if (n.IndexOf("Field", StringComparison.OrdinalIgnoreCase) >= 0) _field = fam;
                    else if (n.IndexOf("Label", StringComparison.OrdinalIgnoreCase) >= 0) _label = fam;
                    else if (n.IndexOf("Display", StringComparison.OrdinalIgnoreCase) >= 0) _display = fam;
                }
            }
            catch
            {
                _pfc = null;
            }

            try
            {
                using (Font f = new Font("Microsoft YaHei UI", 12f, FontStyle.Regular, GraphicsUnit.Pixel))
                    if (f.Name.IndexOf("YaHei", StringComparison.OrdinalIgnoreCase) < 0)
                        _zh = "Microsoft YaHei";
            }
            catch { _zh = "Microsoft YaHei"; }
        }

        private static void AddResource(string name)
        {
            using (System.IO.Stream st = typeof(EfFont).Assembly.GetManifestResourceStream(name))
            {
                if (st == null) return;
                byte[] buf = new byte[st.Length];
                int off = 0;
                while (off < buf.Length)
                {
                    int n = st.Read(buf, off, buf.Length - off);
                    if (n <= 0) break;
                    off += n;
                }
                IntPtr mem = Marshal.AllocCoTaskMem(buf.Length);
                Marshal.Copy(buf, 0, mem, buf.Length);
                _pinned.Add(mem);
                _pfc.AddMemoryFont(mem, buf.Length);
            }
        }

        private static Font Make(FontFamily fam, float px)
        {
            string key = fam.Name + "|" + px.ToString("0.0");
            Font f;
            if (_cache.TryGetValue(key, out f)) return f;
            try
            {
                f = new Font(fam, px, FontStyle.Regular, GraphicsUnit.Pixel);
            }
            catch
            {
                f = new Font(FontFamily.GenericSansSerif, px, FontStyle.Regular, GraphicsUnit.Pixel);
            }
            _cache[key] = f;
            return f;
        }

        /// <summary>拉丁小字 / 字段 / 数字（wdth 112, wght 500）</summary>
        public static Font Field(float px) { return Make(_field, px); }
        /// <summary>眉标 / 标签（wdth 125, wght 600）</summary>
        public static Font Label(float px) { return Make(_label, px); }
        /// <summary>字标 / 大号读数（wdth 125, wght 800）</summary>
        public static Font Display(float px) { return Make(_display, px); }

        /// <summary>中文黑体（粗体用于标题与按钮）</summary>
        public static Font Zh(float px, bool bold)
        {
            string key = _zh + "|" + px.ToString("0.0") + (bold ? "|b" : "|r");
            Font f;
            if (_cache.TryGetValue(key, out f)) return f;
            try
            {
                f = new Font(_zh, px, bold ? FontStyle.Bold : FontStyle.Regular, GraphicsUnit.Pixel);
            }
            catch
            {
                f = new Font("Microsoft YaHei", px, bold ? FontStyle.Bold : FontStyle.Regular, GraphicsUnit.Pixel);
            }
            _cache[key] = f;
            return f;
        }
    }

    /// <summary>无边框窗口底座：拖动、DPI 缩放、动效循环、Esc/Enter、Win11 直角</summary>
    internal class EfForm : Form
    {
        public float S = 1f;
        public bool ReducedMotion;
        public readonly List<Tween> Anims = new List<Tween>();
        private readonly Timer _tick = new Timer();
        private DateTime _last;

        public EfForm()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Theme.Paper;
            ForeColor = Theme.Ink;
            KeyPreview = true;
            AutoScaleMode = AutoScaleMode.None;

            // 任务栏/Alt+Tab 用窗体图标：不显式给，无边框窗体会退回系统默认图标
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch { }

            try { ReducedMotion = !DpiKit.ClientAreaAnimation(); }
            catch { ReducedMotion = false; }

            _tick.Interval = 15;
            _tick.Tick += OnTick;
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            using (Graphics g = CreateGraphics()) S = g.DpiX / 96f;
            if (S <= 0f) S = 1f;
            OnLaidOut();                       // 子类在这里定逻辑尺寸、摆控件
            Region = new Region(new Rectangle(0, 0, ClientSize.Width, ClientSize.Height));
            DpiKit.NoRound(Handle);
            _last = DateTime.UtcNow;
            _tick.Start();
        }

        /// <summary>按逻辑尺寸设窗口大小（子类在构造里调用）</summary>
        protected void SetLogicalSize(float w, float h)
        {
            ClientSize = new Size((int)Math.Round(w * S), (int)Math.Round(h * S));
        }

        /// <summary>DPI 确定后布局子控件（子类覆写）</summary>
        protected virtual void OnLaidOut() { }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            e.Graphics.PixelOffsetMode = PixelOffsetMode.Half;
            e.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
            e.Graphics.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            e.Graphics.ScaleTransform(S, S);
            PaintContent(e.Graphics);
        }

        /// <summary>子类在这里画（逻辑坐标，已按 DPI 缩放）</summary>
        protected virtual void PaintContent(Graphics g) { }

        private void OnTick(object sender, EventArgs e)
        {
            DateTime now = DateTime.UtcNow;
            double dt = (now - _last).TotalMilliseconds;
            _last = now;
            if (dt <= 0) return;
            bool any = false;
            foreach (Tween t in Anims) if (t.Advance(dt)) any = true;
            if (any) Invalidate(true);
        }

        /// <summary>逻辑像素 → 设备像素的矩形</summary>
        public Rectangle PB(float x, float y, float w, float h)
        {
            return new Rectangle(
                (int)Math.Round(x * S), (int)Math.Round(y * S),
                (int)Math.Round(w * S), (int)Math.Round(h * S));
        }

        // ---- 拖动 ----
        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button == MouseButtons.Left) DpiKit.DragMove(this);
        }

        // ---- 键盘 ----
        protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
        {
            if (keyData == Keys.Escape)
            {
                OnEscapeKey();
                return true;
            }
            if (keyData == Keys.Enter)
            {
                OnEnterKey();
                return true;
            }
            return base.ProcessCmdKey(ref msg, keyData);
        }

        protected virtual void OnEscapeKey() { }
        protected virtual void OnEnterKey() { }

        protected override void Dispose(bool disposing)
        {
            if (disposing) _tick.Dispose();
            base.Dispose(disposing);
        }
    }

    /// <summary>Win32 小工具：拖动、Win11 直角</summary>
    internal static class DpiKit
    {
        [DllImport("user32.dll")] private static extern bool ReleaseCapture();
        [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
        [DllImport("dwmapi.dll", PreserveSig = true)]
        private static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int value, int size);
        [DllImport("user32.dll")]
        private static extern bool SystemParametersInfo(int action, int param, ref bool value, int winIni);

        private const int SPI_GETCLIENTAREAANIMATION = 0x1042;

        private const int WM_NCLBUTTONDOWN = 0xA1;
        private const int HTCAPTION = 2;
        private const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;
        private const int DWMWCP_DONOTROUND = 1;

        public static void DragMove(Form f)
        {
            ReleaseCapture();
            SendMessage(f.Handle, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, IntPtr.Zero);
        }

        /// <summary>Win11 默认给顶层窗口加圆角；这套界面一律直角</summary>
        public static void NoRound(IntPtr h)
        {
            try
            {
                int pref = DWMWCP_DONOTROUND;
                DwmSetWindowAttribute(h, DWMWA_WINDOW_CORNER_PREFERENCE, ref pref, sizeof(int));
            }
            catch { }
        }

        /// <summary>系统「显示动画」开关；关掉时所有动效直接到终值</summary>
        public static bool ClientAreaAnimation()
        {
            bool enabled = true;
            try { SystemParametersInfo(SPI_GETCLIENTAREAANIMATION, 0, ref enabled, 0); }
            catch { enabled = true; }
            return enabled;
        }
    }

    /// <summary>切角按钮：悬停时强调色从左擦入，字在边经过时变墨色</summary>
    internal class EfButton : Control
    {
        public enum Kind { Primary, Secondary, Ghost, Danger }

        public Kind Style = Kind.Secondary;
        public string Text2 = "";
        public float Cut = Theme.CutButton;
        public readonly Tween Wipe = new Tween(0f);
        public event EventHandler Activated;
        public EfForm Host;

        private bool _down;

        public EfButton()
        {
            SetStyle(ControlStyles.SupportsTransparentBackColor | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint, true);
            BackColor = Color.Transparent;
            TabStop = true;
            Cursor = Cursors.Hand;
        }

        public void Place(float x, float y, float w, float h, float s)
        {
            Bounds = new Rectangle((int)Math.Round(x * s), (int)Math.Round(y * s),
                (int)Math.Round(w * s), (int)Math.Round(h * s));
        }

        protected override void OnMouseEnter(EventArgs e)
        {
            base.OnMouseEnter(e);
            if (Enabled) Animate(1f);
        }

        protected override void OnMouseLeave(EventArgs e)
        {
            base.OnMouseLeave(e);
            _down = false;
            Animate(0f);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button == MouseButtons.Left) { _down = true; Invalidate(); }
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (e.Button != MouseButtons.Left) return;
            bool was = _down;
            _down = false;
            Invalidate();
            if (was && ClientRectangle.Contains(e.Location) && Activated != null) Activated(this, EventArgs.Empty);
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (e.KeyCode == Keys.Space && Activated != null) Activated(this, EventArgs.Empty);
        }

        protected override void OnGotFocus(EventArgs e) { base.OnGotFocus(e); Invalidate(); }
        protected override void OnLostFocus(EventArgs e) { base.OnLostFocus(e); Invalidate(); }

        private void Animate(float target)
        {
            float ms = (Host != null && Host.ReducedMotion) ? 0f : 280f;
            Wipe.To(target, ms, true);
            if (Wipe.Active && Host != null && !Host.Anims.Contains(Wipe)) Host.Anims.Add(Wipe);
            if (!Wipe.Active) Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            float s = Host != null ? Host.S : 1f;
            g.ScaleTransform(s, s);

            float w = Width / s, h = Height / s;
            RectangleF r = new RectangleF(0, 0, w, h);
            if (_down) r.Offset(0, 1f);

            Color baseFill, baseText, wipeColor;
            switch (Style)
            {
                case Kind.Primary:
                    baseFill = Theme.Ink;
                    baseText = Theme.Dark ? Theme.Paper : Color.White;
                    wipeColor = Theme.Signal;
                    break;
                case Kind.Ghost:
                    baseFill = Color.Empty;
                    baseText = Theme.Ink;
                    wipeColor = Theme.Signal;
                    break;
                case Kind.Danger:
                    baseFill = Theme.Chip;
                    baseText = Theme.Danger;
                    wipeColor = Theme.Danger;
                    break;
                default:
                    baseFill = Theme.Chip;
                    baseText = Theme.Ink;
                    wipeColor = Theme.Signal;
                    break;
            }

            using (GraphicsPath path = Dx.CutPath(r, Corner.BR, Cut))
            {
                GraphicsState st = g.Save();
                g.SetClip(path);
                if (baseFill.A > 0) Dx.Fill(g, r, baseFill);

                float wipe = Wipe.Value;
                if (wipe > 0.005f)
                    Dx.Fill(g, new RectangleF(r.Left, r.Top, r.Width * wipe, r.Height), wipeColor);

                if (Style == Kind.Ghost)
                {
                    float cx = r.Left + r.Width / 2f, cy = r.Top + r.Height / 2f;
                    float k = Math.Min(r.Width, r.Height) * 0.16f;
                    using (Pen p = new Pen(Theme.Ink, 1.6f))
                    {
                        g.DrawLine(p, cx - k, cy - k, cx + k, cy + k);
                        g.DrawLine(p, cx + k, cy - k, cx - k, cy + k);
                    }
                }
                else if (!string.IsNullOrEmpty(Text2))
                {
                    float tw = Dx.Measure(g, Text2, Font);
                    float tx = r.Left + (r.Width - tw) / 2f;
                    float ty = r.Top + (r.Height - Font.GetHeight(g)) / 2f;
                    Dx.Txt(g, Text2, Font, baseText, tx, ty);
                    if (wipe > 0.005f)
                    {
                        GraphicsState s2 = g.Save();
                        g.SetClip(new RectangleF(r.Left, r.Top, r.Width * wipe, r.Height), CombineMode.Intersect);
                        Dx.Txt(g, Text2, Font, Style == Kind.Danger ? Color.White : Theme.OnSignal, tx, ty);
                        g.Restore(s2);
                    }
                }
                g.Restore(st);

                if (Focused)
                {
                    Color ring = Style == Kind.Primary || wipe > 0.5f ? Theme.OnSignal : Theme.Ink;
                    RectangleF fr = new RectangleF(r.Left + 3f, r.Top + 3f, r.Width - 6f, r.Height - 6f);
                    using (GraphicsPath fp = Dx.CutPath(fr, Corner.BR, Math.Max(0f, Cut - 3f)))
                    using (Pen p = new Pen(ring, 2f))
                        g.DrawPath(p, fp);
                }
            }
        }
    }

    /// <summary>方框勾选：勾是折线画的，不是字符</summary>
    internal class EfCheck : Control
    {
        public string Text2 = "";
        public bool Checked;
        public event EventHandler Toggled;
        public EfForm Host;

        public EfCheck()
        {
            SetStyle(ControlStyles.SupportsTransparentBackColor | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint, true);
            BackColor = Color.Transparent;
            TabStop = true;
            Cursor = Cursors.Hand;
        }

        public void Place(float x, float y, float w, float h, float s)
        {
            Bounds = new Rectangle((int)Math.Round(x * s), (int)Math.Round(y * s),
                (int)Math.Round(w * s), (int)Math.Round(h * s));
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (e.Button == MouseButtons.Left && ClientRectangle.Contains(e.Location)) Toggle();
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (e.KeyCode == Keys.Space) Toggle();
        }

        private void Toggle()
        {
            Checked = !Checked;
            if (Toggled != null) Toggled(this, EventArgs.Empty);
            Invalidate();
        }

        protected override void OnGotFocus(EventArgs e) { base.OnGotFocus(e); Invalidate(); }
        protected override void OnLostFocus(EventArgs e) { base.OnLostFocus(e); Invalidate(); }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            float s = Host != null ? Host.S : 1f;
            g.ScaleTransform(s, s);

            float h = Height / s;
            RectangleF box = new RectangleF(0.5f, (h - 18f) / 2f, 18f, 18f);
            Dx.Fill(g, box, Checked ? Theme.Ink : Theme.Panel);
            using (Pen p = new Pen(Theme.Ink, 1f))
            {
                p.Alignment = PenAlignment.Inset;
                g.DrawRectangle(p, box.X, box.Y, box.Width, box.Height);
            }
            if (Checked) Dx.Check(g, new RectangleF(box.X + 2f, box.Y + 2f, 14f, 14f), Theme.Paper, 2f);

            Font f = EfFont.Zh(12.5f, false);
            Dx.Txt(g, Text2, f, Theme.Ink, box.Right + 10f, (h - f.GetHeight(g)) / 2f);

            if (Focused)
            {
                using (Pen p = new Pen(Theme.Ink, 2f))
                    g.DrawRectangle(p, box.X - 2f, box.Y - 2f, box.Width + 4f, box.Height + 4f);
            }
        }
    }

    /// <summary>筛选块：浅灰切角块，选中 = 黄底 + 粗体</summary>
    internal class EfChip : Control
    {
        public string Text2 = "";
        public bool Checked;
        public event EventHandler Activated;
        public EfForm Host;
        private bool _hover;

        public EfChip()
        {
            SetStyle(ControlStyles.SupportsTransparentBackColor | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint, true);
            BackColor = Color.Transparent;
            TabStop = true;
            Cursor = Cursors.Hand;
        }

        public void Place(float x, float y, float w, float h, float s)
        {
            Bounds = new Rectangle((int)Math.Round(x * s), (int)Math.Round(y * s),
                (int)Math.Round(w * s), (int)Math.Round(h * s));
        }

        protected override void OnMouseEnter(EventArgs e) { base.OnMouseEnter(e); _hover = true; Invalidate(); }
        protected override void OnMouseLeave(EventArgs e) { base.OnMouseLeave(e); _hover = false; Invalidate(); }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (e.Button == MouseButtons.Left && ClientRectangle.Contains(e.Location) && Activated != null)
                Activated(this, EventArgs.Empty);
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (e.KeyCode == Keys.Space && Activated != null) Activated(this, EventArgs.Empty);
        }

        protected override void OnGotFocus(EventArgs e) { base.OnGotFocus(e); Invalidate(); }
        protected override void OnLostFocus(EventArgs e) { base.OnLostFocus(e); Invalidate(); }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            float s = Host != null ? Host.S : 1f;
            g.ScaleTransform(s, s);

            float w = Width / s, h = Height / s;
            RectangleF r = new RectangleF(0, 0, w, h);
            using (GraphicsPath path = Dx.CutPath(r, Corner.BR, Theme.CutSmall))
            {
                GraphicsState st = g.Save();
                g.SetClip(path);
                Dx.Fill(g, r, Checked ? Theme.Signal : Theme.Chip);

                Font f = EfFont.Zh(13f, Checked);
                Color c = Checked ? Theme.OnSignal : (_hover ? Theme.Ink : Theme.Ink2);
                Dx.Txt(g, Text2, f, c, 12f, (h - f.GetHeight(g)) / 2f);
                g.Restore(st);

                if (_hover && !Checked)
                {
                    using (Pen p = new Pen(Theme.Ink, 1f))
                        g.DrawRectangle(p, 0.5f, 0.5f, w - 1f, h - 1f);
                }

                if (Focused)
                {
                    RectangleF fr = new RectangleF(3f, 3f, w - 6f, h - 6f);
                    using (GraphicsPath fp = Dx.CutPath(fr, Corner.BR, Math.Max(0f, Theme.CutSmall - 3f)))
                    using (Pen p = new Pen(Theme.Ink, 2f))
                        g.DrawPath(p, fp);
                }
            }
        }
    }
}
