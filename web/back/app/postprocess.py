"""postprocess.py —— 模型输出的二次加工（两个函数，别小看它们）。

1. **表情 token 翻译**：模型输出 `[sns_emoji_012]`，客户端不认识这串东西，
   要换成 `（疑惑）` 才能显示成人话。映射表照抄老后端 37 个表情。
2. **沉浸式过滤**：把 `（…）` 和 `*…*` 里的内容整段删掉，只留台词。

第 2 条看起来像"正则一把梭"，实际上必须**流式安全**：
SSE 每次只来几个字，左括号和右括号很可能落在两个不同的增量里。
所以这里用状态机记深度（`ImmersiveFilter`），而不是对整段文本跑正则。
老后端在这上面翻过车：开关打开反而保留括号——就是拿正则当状态机用了。

提示词层已经要求模型别写括号（见 prompt/rules.py），这层是**兜底**：
模型不听话的时候，用户也不会看到括号。
"""

import re

# 37 个表情的中文名。key 是三位编号（模型可能输出 1~3 位，先 zfill 再查）。
EMOJI_NAME_MAP = {
    "001": "微笑", "002": "星星眼", "003": "惊讶", "004": "得意",
    "005": "可怜", "006": "笑哭", "007": "大哭", "008": "无奈",
    "009": "尴尬", "010": "酷", "011": "大笑", "012": "疑惑",
    "013": "失望", "014": "调皮", "015": "比心", "016": "害羞",
    "017": "笑哭", "018": "冷汗笑", "019": "大笑", "020": "斜眼",
    "021": "晕", "022": "生气", "023": "晕眩", "024": "困",
    "025": "难过", "026": "睡觉", "027": "不满", "028": "闭嘴",
    "029": "思考", "030": "点赞", "031": "祈祷", "032": "鼓掌",
    "033": "爱心", "034": "星星", "035": "愤怒", "036": "满分",
    "037": "加一",
}

EMOJI_TOKEN_PATTERN = re.compile(r"\[sns_emoji_(\d{1,3})\]")

# 全角/半角括号都算动作描写
_OPEN_BRACKETS = "（("
_CLOSE_BRACKETS = "）)"


def translate_emoji_tokens(text: str) -> str:
    """`[sns_emoji_001]` → `（微笑）`。未知编号统一给"表情"。"""

    def replace(match: re.Match[str]) -> str:
        code = match.group(1).zfill(3)
        return f"（{EMOJI_NAME_MAP.get(code, '表情')}）"

    return EMOJI_TOKEN_PATTERN.sub(replace, text)


class ImmersiveFilter:
    """流式安全的动作描写过滤器：吃掉 `（…）` 与 `*…*` 的内容。

    用法：
        f = ImmersiveFilter(enabled=True)
        for delta in stream:  f.feed(delta)        # 每次只吐应该显示的部分
    状态（括号深度、是否在星号里）保存在实例上，所以能跨增量工作。
    """

    __slots__ = ("enabled", "depth", "in_star", "dropped")

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.depth = 0        # 括号嵌套深度，>0 表示"正在丢内容"
        self.in_star = False  # 是否处于 *…* 之间
        self.dropped = 0      # 丢掉多少字（排查用：掉太多说明模型不听话）

    def feed(self, text: str) -> str:
        if not self.enabled or not text:
            return text or ""

        kept: list[str] = []
        for char in text:
            if self.depth > 0:
                self._consume_in_bracket(char)
                continue
            if self.in_star:
                self._consume_in_star(char)
                continue
            if char in _OPEN_BRACKETS:
                self.depth = 1
                self.dropped += 1
                continue
            if char == "*":
                self.in_star = True
                self.dropped += 1
                continue
            kept.append(char)

        return "".join(kept)

    def _consume_in_bracket(self, char: str) -> None:
        """括号里的字符：只维护层级，不输出。"""
        if char in _CLOSE_BRACKETS:
            self.depth -= 1
        elif char in _OPEN_BRACKETS:
            self.depth += 1
        else:
            self.dropped += 1

    def _consume_in_star(self, char: str) -> None:
        """星号里的字符：遇到配对的 * 就退出，其余丢弃。"""
        if char == "*":
            self.in_star = False
        else:
            self.dropped += 1


def strip_immersive(text: str, enabled: bool = True) -> str:
    """非流式版本：一次去掉动作描写，顺手收拾留下的空行和多余空格。"""
    if not enabled or not text:
        return text or ""
    out = ImmersiveFilter(True).feed(text)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def finalize_reply(text: str, *, immersive: bool) -> str:
    """出话之后的统一收尾：先翻译表情，再（可选）过滤括号。

    顺序不能反：表情会被翻译成 `（微笑）`，如果先过滤括号，这些"表情"也会被当成动作描写删掉。
    """
    return strip_immersive(translate_emoji_tokens(text), enabled=immersive)


__all__ = [
    "EMOJI_NAME_MAP",
    "ImmersiveFilter",
    "finalize_reply",
    "strip_immersive",
    "translate_emoji_tokens",
]
