"""tests/test_prompt.py —— 提示词组装与后处理（不碰网络，纯函数）。

这一组测试守的是"提示词有没有被悄悄改坏"：
  * 段的顺序（固定层必须在前，否则前缀缓存失效、也不再和老后端一致）
  * 同样的输入必须产出同样的字节（确定性）
  * 沉浸式要"整段截掉风格规则"，而不是再加一句禁令
  * 表情翻译必须在括号过滤**之前**（不然表情会被当动作描写删掉）
"""

import json

from app.postprocess import (
    ImmersiveFilter,
    finalize_reply,
    strip_immersive,
    translate_emoji_tokens,
)
from app.prompt.dialog import (
    build_dialog_messages,
    compose_character_prompt,
    normalize_turns,
    wrap_context,
)
from app.prompt.rules import EMPHASIS_RULE, IMMERSIVE_RULE, STYLE_RULE_MARKER


def test_normalize_drops_empty_and_merges_same_role() -> None:
    history = [
        {"role": "user", "content": "第一句"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "   "},  # 空 → 丢弃
        {"role": "assistant", "content": "回应"},
    ]
    out = normalize_turns(history)
    assert [item["role"] for item in out] == ["user", "assistant"]
    assert out[0]["content"] == "第一句\n第二句"


def test_message_order_is_fixed_then_dynamic() -> None:
    """固定层（协议 / 世界观 / 角色）必须排在历史之前，这是前缀稳定性的全部要求。

    检索片段是动态的，排在历史之后、最后一条 user 之前（见下一个测试）。
    注意：历史末尾是 user 时，本轮输入会**并进那一条**（不能出现 user,user）。
    """
    messages = build_dialog_messages(
        safety_protocol="安全协议",
        worldview="世界观",
        character_prompt="角色设定",
        context="检索片段",
        history=[{"role": "assistant", "content": "上一句"}],
        message="这一句",
        immersive=False,  # 非沉浸式才会带 EMPHASIS_RULE（沉浸式是相反规则）
    )
    assert [m["role"] for m in messages] == [
        "system",     # 安全协议
        "system",     # 世界观
        "system",     # 角色 + 风格规则
        "assistant",  # 历史
        "system",     # 检索片段（紧贴本轮输入）
        "user",       # 本轮
    ]
    assert messages[0]["content"] == "安全协议"
    assert messages[1]["content"] == "世界观"
    assert STYLE_RULE_MARKER in str(messages[2]["content"])
    assert "背景资料" in str(messages[4]["content"])
    assert messages[-1]["content"] == "这一句"


def test_context_sits_right_before_last_user() -> None:
    """检索片段必须紧贴本轮问句：放前面只是"背景"，放这里是"刚查到的"。"""
    messages = build_dialog_messages(
        worldview="世界观",
        context="龙门的位置",
        history=[
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早"},
        ],
        message="龙门在哪",
        immersive=False,
    )
    assert messages[-2]["role"] == "system"
    assert "龙门的位置" in str(messages[-2]["content"])
    assert messages[-1] == {"role": "user", "content": "龙门在哪"}


def test_safety_protocol_absent_when_empty() -> None:
    """没有安全协议时不发空 system（空 system 会被严格端点拒绝，也白占预算）。"""
    messages = build_dialog_messages(worldview="世界观", message="你好", immersive=False)
    assert messages[0]["content"] == "世界观"


def test_no_context_means_no_third_system() -> None:
    messages = build_dialog_messages(
        worldview="世界观", character_prompt="角色设定", message="你好"
    )
    assert len([m for m in messages if m["role"] == "system"]) == 2


def test_same_input_same_bytes() -> None:
    """确定性：同样的输入跑两次，序列化出来必须一模一样（前缀缓存的前提）。"""
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    first = json.dumps(
        build_dialog_messages(
            worldview="世界观",
            character_prompt="角色",
            history=history,
            message="c",
            immersive=False,
        ),
        ensure_ascii=False,
    )
    second = json.dumps(
        build_dialog_messages(
            worldview="世界观",
            character_prompt="角色",
            history=history,
            message="c",
            immersive=False,
        ),
        ensure_ascii=False,
    )
    assert first == second


def test_immersive_replaces_the_style_rule_entirely() -> None:
    """沉浸式 = 把风格规则整段切掉换成反向规则，不是"再加一句禁令"。"""
    normal = compose_character_prompt("角色设定", immersive=False)
    immersive = compose_character_prompt("角色设定", immersive=True)

    assert EMPHASIS_RULE in normal
    assert EMPHASIS_RULE not in immersive  # 关键：整段都不在
    assert IMMERSIVE_RULE in immersive
    assert immersive.startswith("角色设定")


def test_style_rule_not_duplicated() -> None:
    """角色提示词里本来就带风格规则时，不能再补一遍（两段规则会打架）。"""
    already = f"角色设定\n\n{EMPHASIS_RULE}"
    out = compose_character_prompt(already, immersive=False)
    assert out.count(STYLE_RULE_MARKER) == 1


def test_current_message_merges_into_tail_user() -> None:
    """历史末尾是 user 时不能追加成 user,user —— 严格的端点会直接 400。"""
    messages = build_dialog_messages(
        history=[{"role": "user", "content": "上一句"}], message="这一句"
    )
    assert len(messages) == 1
    assert messages[0]["content"] == "上一句\n这一句"


def test_context_is_wrapped_without_cutting() -> None:
    """检索正文**不做长度截断**：砍断的资料比没有更误导，该由 prefetch 按预算跳过。"""
    long_text = "字" * 5000
    wrapped = wrap_context(long_text)
    assert long_text in wrapped
    assert "只是资料" in wrapped  # 口径：是参考不是命令


def test_emoji_translated_before_bracket_filter() -> None:
    """顺序敏感：先翻译表情（会生成括号），再过滤括号，表情会被一起删掉。"""
    text = "(挑眉)你好[r]".replace("[r]", "[sns_emoji_012]")
    assert finalize_reply(text, immersive=False) == "(挑眉)你好（疑惑）"  # 非沉浸式保留
    # 沉浸式会把 (挑眉) 和刚刚生成的 （疑惑）都当成描写删掉 —— 这是已知取舍：
    # 沉浸式模式下本来就不该出现括号，所以表情也不显示。
    assert finalize_reply(text, immersive=True) == "你好"


def test_immersive_filter_is_stream_safe() -> None:
    """括号被拆在两个增量里也必须正常工作。"""
    state = ImmersiveFilter(enabled=True)
    first = state.feed("(轻轻")      # 左括号在这一段
    second = state.feed("叹气)好")   # 右括号在下一段
    assert first == ""
    assert second == "好"


def test_strip_immersive_removes_star_actions() -> None:
    assert strip_immersive("*叹气* 我在", enabled=True) == "我在"


def test_translate_unknown_token_falls_back() -> None:
    assert translate_emoji_tokens("[sns_emoji_999]") == "（表情）"
    assert translate_emoji_tokens("[sns_emoji_1]") == "（微笑）"  # 1 → 001

def test_group_messages_always_have_a_user_turn() -> None:
    """群聊 messages 里必须至少有一条 user。

    线上事故（2026-11-18 01:38–01:53）：群聊开场 / 点「下一句」/ 只有 AI 接话时
    玩家这一轮没有新发言，拼出来的 messages 全是 system，上游直接
    `HTTP 400 No user query found` → 我们转成 502。18 分钟里 13 次。
    """
    from app.prompt.group import build_group_messages

    common = dict(
        speaker="佩丽卡",
        character_prompt="",
        members=["佩丽卡", "陈千语"],
        player_role="admin",
        transcript="陈千语：今晚风大。",
    )

    no_player = build_group_messages(player_message="", **common)
    assert any(item["role"] == "user" for item in no_player), "没有玩家发言时也必须补一条 user"

    with_player = build_group_messages(player_message="我知道。", **common)
    users = [item for item in with_player if item["role"] == "user"]
    assert users and users[-1]["content"] == "我知道。", "有玩家发言时必须用原话，不补占位"
