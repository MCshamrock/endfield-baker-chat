// Notes.cs —— 安装器主界面上「本次更新」卡片里的条目
//
// 这是**手改文件**（不由 build.py 生成）：每次发版只改这里，排版代码不用碰。
// 每行 = { 编号, 一句话说明, 右侧分类标签 }
//   · 建议 3~4 条，每条中文不超过 ~20 字（卡片宽度有限，太长会顶到右边标签）
//   · 写"用户能感知到什么"，不要写内部实现（"窗口化渲染"这种词留给发版说明）
//   · 右侧标签 2~4 个字，用来分类：导出 / 性能 / 界面 / 数据 …
namespace BakerSetup
{
    internal static class Notes
    {
        /// <summary>卡片标题</summary>
        public const string Title = "本次更新";

        /// <summary>卡片里逐行显示的更新摘要（从上到下）</summary>
        public static readonly string[][] Rows = new string[][]
        {
            new string[] { "No.01", "多行回复不再被截断", "修复" },
            new string[] { "No.02", "气泡高度按真实行数算", "修复" },
            new string[] { "No.03", "干员记忆暂时关闭（数据保留）", "调整" },
        };
    }
}
