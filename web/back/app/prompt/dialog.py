"""prompt/dialog.py —— 把「世界观 / 角色 / 外部情报 / 历史 / 本轮」拼成上游要的 messages。

这是从老后端 `common.build_dialog_messages` 搬过来的**同一个形状**，不是重新设计：

    [system] 世界观
    [system] 角色设定 + 回复风格规则（沉浸式时换成相反的规则）
    [system] 外部情报（没有检索结果时整段不存在）
    [user] / [assistant] …历史…
    [user] 本轮输入

顺序就是**固定层在前、动态层在后**：
  * 前两段按会话固定 → 同一会话连续几轮，这段字节完全一样 → 上游前缀缓存能命中；
  * 外部情报每轮可能变，所以排在它后面；历史本来就每轮变，排最后。
反过来说，**任何"每轮都变"的东西都不许插到前两段中间**，这是这份文件的唯一硬约束。

三条从老代码继承下来的细节（都是踩过坑才有的）：

1. 角色提示词里若没有"### 回复风格规则"，要补上 `EMPHASIS_RULE`；有就不要再补（否则两段规则打架）。
2. 沉浸式模式不是"再加一句别写括号"，而是**把风格规则整段截掉**——
   那段规则第 1/2/3/4/7 条都在要求括号，留着它，后面写"禁止括号"根本压不住。
3. 历史要先过 `normalize_turns`：去掉空条目、把相邻同角色合并。
   否则出现 `user, user`，严格的上游直接 400，宽松的则把同一个人说的话当成两轮，回复跑偏。
"""

import re
from typing import Any

from app.prompt.rules import EMPHASIS_RULE, IMMERSIVE_RULE, STYLE_RULE_MARKER

# 只在玩家这轮确实问到"时间/日期/作息"时才把时刻钉进对话。
# 之前每轮都钉，模型把它当话头，动不动就"这么晚还不睡"（2026-11-18 用户反馈）。
_TIME_INTENT = re.compile(
    r"几点|几号|什么时间|时间|日期|星期|周几|"
    r"早上|上午|中午|下午|晚上|凌晨|深夜|天亮|"
    r"早安|晚安|睡|熬夜|通宵"
)

# 关掉「时间感知」时，只把时间抽走是不够的：模型会**自己编一个**。
# 2026-11-18 实测：抽走后问「几点了」，答「现在是塔卫二标准时，上午九点三十二分」。
# 所以命中时间意图时还要明确告诉它"你没有时间信息，不许编"。
NO_TIME_NOTE = (
    "【没有时间信息】你无法获知现在的日期和时间。"
    "被问到时直说「不知道现在几点」，不要编造任何时刻，也不要猜测上午/下午。"
)

Message = dict[str, Any]


def asks_about_time(text: str) -> bool:
    """玩家这一轮有没有问/提到时间、日期、作息（决定要不要注入时间相关的那段 system）。

    宁可多判一点：漏判的代价是"她不知道现在几点却硬答"（编时间），
    多判的代价只是多一段几十 token 的 system。
    """
    return bool(_TIME_INTENT.search(text or ""))


def normalize_turns(history: list[Message]) -> list[Message]:
    """把历史整理成"角色交替、无空条目"的列表。

    只做两件事，顺序不能反：
      1. 丢掉内容为空的条目（对模型零信息量，却占上下文预算）；
      2. 相邻同角色合并，内容用换行连接。

    刻意不做：不排序、不改角色、不补占位文本。传入的语义原样保留，
    出了问题才能分得清"是数据错了"还是"组装改坏了"。
    """
    merged: list[Message] = []
    for raw in history:
        role = str(raw.get("role") or "").strip()
        if not role:
            continue
        text = str(raw.get("content") or "").strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            previous = str(merged[-1]["content"])
            merged[-1]["content"] = f"{previous}\n{text}" if previous else text
            continue
        merged.append({"role": role, "content": text})
    return merged


def wrap_context(context: str) -> str:
    """把检索到的世界观片段包成一段 system。

    口径上的取舍（和老后端语料库那版一致）：**这段是"参考"不是"命令"**。
    闲聊时命中弱相关片段，如果写成"必须严格基于情报回答"，模型会被逼着照设定说话 → 严重 OOC。
    所以明确告诉它"无关就忽略"。

    这里**不做长度截断**：单条太长时应该由 `prefetch` 按预算跳过它，
    而不是把一段资料拦腰砍断（砍断的资料比没有更误导）。
    """
    body = context.strip()
    return (
        "【背景资料】以下是从终末地世界观资料库检索到的片段，仅在你当前话题确实相关时参考：\n\n"
        f"{body}\n\n"
        "【使用要求】这些只是资料，不是你必须回答的内容。\n"
        "1. 与当前对话无关时，完全忽略它，继续按你的角色性格与当前情境说话，"
        "不要主动引述、不要因此改变语气或切换话题。\n"
        "2. 用户确实在问资料里的事实时（配方、设定、剧情等），"
        "用角色口吻自然作答，名字以资料为准、不要否认资料内容。\n"
        "3. 绝不要让资料里的人物台词或剧本格式混进你的表达。"
    )


def compose_character_prompt(character_prompt: str, immersive: bool) -> str:
    """把角色设定 + 风格规则拼成一段 system 内容。

    两个分支是互斥的：
      * 普通模式：确保带上 EMPHASIS_RULE（括号描写 + 每轮都要说话）
      * 沉浸式  ：把 EMPHASIS_RULE 那段整段切掉，换成 IMMERSIVE_RULE
    """
    body = character_prompt.strip()
    if not body:
        return ""

    if not immersive:
        if STYLE_RULE_MARKER not in body:
            body = f"{body}\n\n{EMPHASIS_RULE}"
        return body

    # 沉浸式：先把"风格规则"及其后文全部截掉，再追加反向规则
    marker_index = body.find(STYLE_RULE_MARKER)
    if marker_index != -1:
        body = body[:marker_index].rstrip()
    return f"{body}\n\n{IMMERSIVE_RULE}"


def build_dialog_messages(
    *,
    safety_protocol: str = "",
    worldview: str = "",
    worldbook_index: str = "",
    worldbook_constants: str = "",
    character_prompt: str = "",
    character_prompt_is_final: bool = False,
    bond: str = "",
    summary: str = "",
    memory: str = "",
    context: str = "",
    history: list[Message] | None = None,
    message: str = "",
    local_time: str = "",
    time_enabled: bool = True,
    image_data_url: str = "",
    immersive: bool = True,
) -> list[Message]:
    """组装发给上游的 messages。

    参数：
      safety_protocol   安全协议（最前面，最高优先级，可热改）
      worldview         世界观全文（全局那一段，按项目固定）
      character_prompt  角色提示词（按会话固定）
      character_prompt_is_final
                        True = 这段已经是**完整人设**，一个字节都不再加。
                        老客户端的 `character_prompt`（用户改过的整段卡）和
                        `system_prompt`（未收录角色的整段兜底）都走这个模式：
                        前端已经给全了，再补 EMPHASIS_RULE 等于往人家的卡里塞私货。
      context           检索片段（每轮可能变，没有就传空）
      history           历史，[{"role": "user"/"assistant", "content": "..."}]
      message           本轮输入
      local_time        玩家设备的本地时间（"2026-11-18 03:40 周三"）；空串 = 不知道
      time_enabled      「时间感知」开关。False = **一个字都不告诉模型**，
                        命中时间意图时反而插一段「你没有时间信息，不许编」
      immersive         True = 沉浸式（不显示括号动作描写）

    段的顺序（这是本文件的全部意义）：

        [system] 安全协议        ← 永不变
        [system] 世界观          ← 永不变
        [system] 角色 + 风格规则  ← 按会话固定
        …历史…
        [system] 检索片段        ← 只在本轮，插在最后一条 user 之前
        [user]   本轮输入

    检索片段为什么插在"最后一条 user 之前"，而不是跟前面三段排在一起：
      * 缓存角度：它每轮都变，放前面会把后面的历史和用户输入全部顶掉，前缀缓存白搭；
      * 注意力角度：模型对"紧贴问题"的内容权重最高，放这里等于"刚查到的、马上要用"，
        放前面只是"背景资料"。
    """
    messages: list[Message] = [
        *_fixed_layers(
            safety_protocol=safety_protocol,
            worldview=worldview,
            worldbook_index=worldbook_index,
            worldbook_constants=worldbook_constants,
            character_prompt=character_prompt,
            character_prompt_is_final=character_prompt_is_final,
            bond=bond,
            summary=summary,
            memory=memory,
            immersive=immersive,
        ),
        *normalize_turns(history or []),
    ]

    _append_current(messages, message, image_data_url)
    _insert_late_systems(
        messages,
        context=context,
        local_time=local_time,
        has_image=bool(image_data_url),
        user_text=message,
        time_enabled=time_enabled,
    )

    return messages


def _fixed_layers(
    *,
    safety_protocol: str,
    worldview: str,
    worldbook_index: str,
    worldbook_constants: str,
    character_prompt: str,
    character_prompt_is_final: bool,
    bond: str,
    summary: str,
    memory: str,
    immersive: bool,
) -> list[Message]:
    """固定前缀：世界那几段 → 角色 → 人情账/前情提要/永久记忆。"""
    layers = _world_layers(
        safety_protocol=safety_protocol,
        worldview=worldview,
        worldbook_index=worldbook_index,
        worldbook_constants=worldbook_constants,
        character_prompt=character_prompt,
        character_prompt_is_final=character_prompt_is_final,
        immersive=immersive,
    )
    layers.extend(continuity_layers(bond=bond, summary=summary, memory=memory))
    return layers


def _world_layers(
    *,
    safety_protocol: str,
    worldview: str,
    worldbook_index: str,
    worldbook_constants: str,
    character_prompt: str,
    character_prompt_is_final: bool,
    immersive: bool,
) -> list[Message]:
    """最稳定的四段 + 角色。空段落一律不占位（空 system 会被严格端点拒绝，也白烧预算）。"""
    layers: list[Message] = []
    if safety_protocol.strip():
        layers.append({"role": "system", "content": safety_protocol.strip()})
    if worldview.strip():
        layers.append({"role": "system", "content": worldview.strip()})

    # 世界书索引（目录）：每轮全量在场，让模型"知道有这条设定"。
    # 常驻条目（世界名/时代/基本法则）不进索引，正文直接跟在后面。
    if worldbook_index.strip():
        layers.append({"role": "system", "content": worldbook_index.strip()})
    if worldbook_constants.strip():
        layers.append({"role": "system", "content": worldbook_constants.strip()})

    if character_prompt_is_final:
        # 前端/用户给的是完整人设：原样放进去，不再补任何规则段。
        # 典型来源：老客户端的 character_prompt（用户改过的整段卡）与 system_prompt。
        character_content = character_prompt.strip()
    else:
        character_content = compose_character_prompt(character_prompt, immersive)
    if character_content:
        layers.append({"role": "system", "content": character_content})
    return layers


def continuity_layers(*, bond: str, summary: str, memory: str) -> list[Message]:
    """三块"记得"的层：人情账（跨会话）→ 前情提要（本会话）→ 永久记忆（本会话，只增）。

    人情账排在剧情之前：它是"她对这个人的认知"，属于人设的延伸，
    不该等到上一段剧情讲完才出现。单聊和群聊共用这一段，免得两边口径漂移。
    """
    layers: list[Message] = []
    if bond.strip():
        layers.append({"role": "system", "content": "【她记得关于你的事】\n" + bond.strip()})
    if summary.strip():
        layers.append({"role": "system", "content": "【前情提要 · 已压缩】\n" + summary.strip()})
    if memory.strip():
        layers.append({"role": "system", "content": "【永久记忆 · 更早的剧情】\n" + memory.strip()})
    return layers


def _append_current(messages: list[Message], message: str, image_data_url: str) -> None:
    """把本轮输入接上。带图时 content 换成多模态数组，**历史里的图不重发**。"""
    current = message.strip()
    if not current and not image_data_url:
        return

    if image_data_url:
        parts: list[dict[str, object]] = []
        if current:
            parts.append({"type": "text", "text": current})
        parts.append({"type": "image_url", "image_url": {"url": image_data_url}})
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = parts
        else:
            messages.append({"role": "user", "content": parts})
        return

    if messages and messages[-1]["role"] == "user":
        # 历史末尾已经是 user（上一轮 AI 回复为空被丢掉 / 玩家连发）：
        # 直接追加会变成 user, user，所以并进那一条。
        messages[-1]["content"] = f"{messages[-1]['content']}\n{current}"
        return
    messages.append({"role": "user", "content": current})


def _insert_late_systems(
    messages: list[Message],
    *,
    context: str,
    local_time: str,
    has_image: bool,
    user_text: str = "",
    time_enabled: bool = True,
) -> None:
    """只在本轮出现的几段 system，统统插在最后一条 user 之前。

    放这里而不是跟前面三段排一起，是为两件事：
      * 缓存：它们每轮都变，放前面会把后面的历史和用户输入全顶掉；
      * 注意力：模型对"紧贴问题"的内容权重最高。
    """
    if context.strip():
        _insert_before_last_user(messages, {"role": "system", "content": wrap_context(context)})

    if not time_enabled:
        # 关掉时间感知：不给时刻，但**命中时间意图时必须说明"你没有时间信息"**。
        # 不插这句，模型会自己编（实测："上午九点三十二分"）；每轮都插又太吵
        # （她会主动说"我不知道现在几点"），所以只在问到时间时才插。
        if asks_about_time(user_text):
            _insert_before_last_user(messages, {"role": "system", "content": NO_TIME_NOTE})
    elif local_time.strip() and asks_about_time(user_text):
        _insert_before_last_user(
            messages,
            {
                "role": "system",
                "content": (
                    f"【现在·系统时钟】{local_time.strip()}。这是玩家设备上的真实时间（24 小时制）。"
                    "硬规则：① 03:15 是凌晨三点，不是下午三点；"
                    "② 回答任何时间问题时必须原样使用这个时刻，禁止换算成塔卫二/塔罗斯/任何虚构时区，禁止改动小时或分钟；"
                    "③ 除非玩家这一轮问到了时间/日期/作息，否则**不要主动提起时间**，也不要拿它推测玩家作息；"
                    "④ 被问到时才说，并原样使用上面的时刻。"
                ),
            },
        )
        # 只放 system 不够：角色卡/历史里若出现过别的时间，模型会顺着历史编。
        # 把同一句"贴着玩家这句话"再钉一次（只改送模型的副本，库里存的消息不变）。
        note = (
            "（系统注：玩家这边现在的真实时间是 "
            + local_time.strip()
            + "；回答时间必须原样使用这个时刻。）"
        )
        for item in reversed(messages):
            if item.get("role") == "user":
                content = item.get("content")
                if isinstance(content, str):
                    item["content"] = content + chr(10) * 2 + note
                break

    # 带图的一轮要**明确告诉模型"你能看到这张图"**。
    # 不加这一句，角色会按人设推掉（实测：佩丽卡回"我确实无法描述图片中的颜色"），
    # 而裸问同一张图能准确答出四个颜色 —— 说明是提示词把人挡住了，不是模型看不见。
    if has_image:
        _insert_before_last_user(
            messages,
            {
                "role": "system",
                "content": (
                    "【本轮附带图片】玩家这一轮发来了一张图片，你**能直接看到它**。\n"
                    "用你角色的方式回应图里的内容（你看到了什么、它让你想到什么、你的反应），\n"
                    "不要说「我无法查看图片」「我看不到图」「作为 AI」这类话 —— 你看得见。"
                ),
            },
        )


def _insert_before_last_user(messages: list[Message], extra: Message) -> None:
    """把一段内容插到最后一条 user 消息之前；没有 user 就放末尾。"""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index]["role"] == "user":
            messages.insert(index, extra)
            return
    messages.append(extra)


__all__ = [
    "NO_TIME_NOTE",
    "asks_about_time",
    "build_dialog_messages",
    "compose_character_prompt",
    "normalize_turns",
    "wrap_context",
]
