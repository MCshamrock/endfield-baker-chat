// PromptForm.cs —— 安装器自己的确认弹窗（endfield-ui 风格，不用系统 MessageBox）
//
// 用途：需要管理员权限时询问是否重启、检测到旧版本时询问保留数据更新。
// 结构：三色条 → 眉标 → 标题 → 正文（自动换行）→ 右下角按钮组。
// 返回：Selected = 按下按钮的下标；Esc / 关窗 = -1。

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;
using BakerUi;

namespace BakerSetup
{
    internal class PromptForm : EfForm
    {
        private const float Fw = 660f;
        private const float Fh = 316f;
        private const float L = 32f;
        private const float R = Fw - 32f;

        private readonly string _eyebrow;
        private readonly string _title;
        private readonly string _body;
        private readonly string[] _items;
        private readonly List<EfButton> _buttons = new List<EfButton>();
        private readonly List<int> _widths = new List<int>();

        private readonly Tween _strip = new Tween(0f);

        public int Selected = -1;

        public PromptForm(string eyebrow, string title, string body, string[] items)
        {
            _eyebrow = eyebrow ?? "";
            _title = title ?? "";
            _body = body ?? "";
            _items = items ?? new string[0];

            Text = title;
            StartPosition = FormStartPosition.CenterParent;   // 压在安装器窗口中间
            Anims.Add(_strip);

            for (int i = 0; i < _items.Length; i++)
            {
                EfButton b = new EfButton();
                b.Host = this;
                b.Style = i == 0 ? EfButton.Kind.Primary : EfButton.Kind.Secondary;
                b.Text2 = _items[i];
                b.Font = EfFont.Zh(i == 0 ? 15f : 14f, true);
                int idx = i;
                b.Activated += delegate { Selected = idx; Close(); };
                Controls.Add(b);
                _buttons.Add(b);
            }
        }

        protected override void OnLaidOut()
        {
            SetLogicalSize(Fw, Fh);

            float x = R;
            for (int i = _buttons.Count - 1; i >= 0; i--)
            {
                EfButton b = _buttons[i];
                float w;
                using (Bitmap bmp = new Bitmap(1, 1))
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
                    w = Dx.Measure(g, b.Text2, b.Font) + 44f;
                }
                if (w < 104f) w = 104f;
                x -= w;
                b.Place(x, Fh - 76f, w, 48f, S);
                x -= 12f;
            }

            ApplyAccent();
        }

        private void ApplyAccent()
        {
            try
            {
                if (ActiveControl == null && _buttons.Count > 0) ActiveControl = _buttons[_buttons.Count - 1];
            }
            catch { }
        }

        public void PlayEntrance()
        {
            if (ReducedMotion) { _strip.To(1f, 0f, false); Invalidate(true); return; }
            _strip.To(0f, 0f, false);
            _strip.To(1f, 300f, true);
            Invalidate(true);
        }

        protected override void OnEscapeKey()
        {
            Selected = -1;
            Close();
        }

        protected override void OnEnterKey()
        {
            Selected = 0;
            Close();
        }

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

            Dx.Eyebrow(g, _eyebrow, EfFont.Label(11f), Theme.Ink2, L, 40f);
            Dx.Txt(g, _title, EfFont.Zh(22f, true), Theme.Ink, L, 54f);
            Dx.HLine(g, L, R, 102f, Theme.Rule);

            Font body = EfFont.Zh(12.5f, false);
            float y = 124f;
            foreach (string line in _body.Replace("\r\n", "\n").Split('\n'))
            {
                foreach (string seg in Dx.Wrap(g, line, body, R - L))
                {
                    Dx.Txt(g, seg, body, Theme.Ink, L, y);
                    y += 24f;
                }
            }

            // 左下角一行小字：说明这次操作不会动聊天记录
            Dx.Txt(g, "Esc 关闭", EfFont.Field(10.5f), Theme.Ink2, L, Fh - 52f);

            using (Pen p = new Pen(Theme.Ink, 1f))
            {
                p.Alignment = PenAlignment.Inset;
                g.DrawRectangle(p, 0, 0, Fw - 1f, Fh - 1f);
            }
        }
    }
}
