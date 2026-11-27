"""prompt/group.py —— 群聊的提示词：一条规则段 + 一份"群聊记录"。

群聊和单聊的差别不在"模型更强"，而在**约束更多**：
  * 只能替一个人说话（不然一条回复里三个人对话，玩家没法接）；
  * 要知道群里还有谁、跟他什么关系（不然人人都是客服腔）；
  * 要短（群聊里没人发小作文）。

所以这里比单聊多两段：
  * `【群聊场景】`：成员名单 + 玩家身份 + "只输出你一个人的话"
  * `【群聊记录】`：把多轮对话压成"名字：内容"的文本，而不是严格的 user/assistant 交替
    （群聊本来就不交替，硬套 role 会让模型以为只有两个人在说话）

历史为什么不用 messages 交替：`normalize_turns` 会把相邻同角色的发言合并 ——
在群聊里那等于把两个人的话粘成一句，直接串台。
"""

from app.prompt.dialog import NO_TIME_NOTE, Message, asks_about_time

GROUP_RULES = """【群聊场景】
这是一个多人聊天群{group_name}，成员：{members}。
{player_line}
你正在扮演其中的「{speaker}」。
规则：
1. 只输出「{speaker}」这一个角色的发言，绝对不要替别人说话，也不要写旁白式的第三人称叙述。
2. 这是群聊，不是一对一。语气要像在群里说话：短、口语化，可以点名回应某个人（用「@某某」或直接叫名字），不要长篇大论。
3. 发言要接得住上文，针对最近几条消息里最值得回应的点，不要自说自话，也不要把之前说过的话重复一遍。
4. 与在场成员的关系、称呼、态度，严格按上面给出的人设与关系来。"""


def group_rules_text(
    *, speaker: str, members: list[str], player_role: str, group_name: str = ""
) -> str:
    """拼【群聊场景】那一段。"""
    if player_role == "observer":
        player_line = "玩家正在旁观，不参与对话。"
    elif player_role == "admin":
        player_line = "玩家以「管理员」身份参与对话，管理员的发言由玩家自己输入。"
    else:
        player_line = f"玩家扮演「{player_role}」，该角色的发言由玩家自己输入。"

    return GROUP_RULES.format(
        group_name=f"「{group_name}」" if group_name else "",
        members="、".join(members),
        player_line=player_line,
        speaker=speaker,
    )


def build_transcript(messages: list[tuple[str, str]], limit: int = 0) -> str:
    """把消息列表压成"名字：内容"的文本（正序）。

    `messages` 是 [(发言人显示名, 内容)]；发言人空 = 玩家自己。

    `limit=0` 表示**全部**。默认就是全部：群聊的记忆完整性不该由条数决定，
    真要控长度只能靠压缩（`memory.compress_to_fit`），不能靠砍条数。
    """
    recent = messages[-limit:] if limit > 0 else messages
    lines = []
    for speaker, text in recent:
        who = speaker.strip() or "玩家"
        lines.append(f"{who}：{text.strip()}")
    return "\n".join(lines)


def build_group_messages(
    *,
    safety_protocol: str = "",
    worldview: str = "",
    speaker: str,
    character_prompt: str,
    members: list[str],
    player_role: str,
    group_name: str = "",
    transcript: str = "",
    bond: str = "",
    summary: str = "",
    memory: str = "",
    context: str = "",
    nudge: str = "",
    player_message: str = "",
    local_time: str = "",
    time_enabled: bool = True,
    immersive: bool = True,
) -> list[Message]:
    """群聊发给上游的 messages。

    顺序：协议 → 世界观 → 群聊场景 → 角色 → 前情提要 → 永久记忆 → 群聊记录 → 玩家这句。
    "群聊记录" 是一整段 system，不是逐条 user/assistant —— 原因见文件头。

    `nudge` 是"该续接"的提示（smart 模式里主持人判 END 时用，见老协议）：
    它是一段 system，插在玩家这句之前，不改变上面任何一段的位置。
    """
    layers = _group_layers(
        safety_protocol=safety_protocol,
        worldview=worldview,
        speaker=speaker,
        character_prompt=character_prompt,
        members=members,
        player_role=player_role,
        group_name=group_name,
        transcript=transcript,
        bond=bond,
        summary=summary,
        memory=memory,
        context=context,
        nudge=nudge,
        immersive=immersive,
    )
    if not time_enabled:
        # 同单聊：关掉时间感知时，问到时间就明确说"你没有时间信息"，否则模型会编
        if asks_about_time(player_message):
            layers.append({"role": "system", "content": NO_TIME_NOTE})
    elif local_time.strip():
        layers.append(
            {
                "role": "system",
                "content": (
                    f"【现在·系统时钟】{local_time.strip()}。这是玩家设备上的真实时间（24 小时制）。"
                    "硬规则：① 03:15 是凌晨三点，不是下午三点；"
                    "② 回答任何时间问题时必须原样使用这个时刻，禁止换算成塔卫二/塔罗斯/任何虚构时区，禁止改动小时或分钟；"
                    "③ 可以顺带说早安/晚安，但不要主动报时。"
                ),
            }
        )
    if player_message.strip():
        layers.append({"role": "user", "content": player_message.strip()})
    else:
        # 群聊"玩家这一轮没说话"是常态：开场、点「下一句」、只有 AI 接话的时候。
        # 但上游要求 messages 里至少有一条 user，否则整份只有 system → HTTP 400
        # "No user query found" → 我们转成 502（线上实测 01:38–01:50 有 13 次）。
        # 所以补一条中性的"继续"指令；措辞刻意让模型别把这句当台词复述。
        layers.append(
            {
                "role": "user",
                "content": "（玩家这一轮没有新发言。接着上面的对话往下演，不要复述这句话。）",
            }
        )
    return layers


def _group_layers(
    *,
    safety_protocol: str,
    worldview: str,
    speaker: str,
    character_prompt: str,
    members: list[str],
    player_role: str,
    group_name: str,
    transcript: str,
    bond: str,
    summary: str,
    memory: str,
    context: str,
    nudge: str,
    immersive: bool,
) -> list[Message]:
    """群聊的所有 system 段（不含玩家这一句）。"""
    from app.prompt.dialog import compose_character_prompt, continuity_layers, wrap_context

    layers: list[Message] = []
    if safety_protocol.strip():
        layers.append({"role": "system", "content": safety_protocol.strip()})
    if worldview.strip():
        layers.append({"role": "system", "content": worldview.strip()})

    layers.append(
        {
            "role": "system",
            "content": group_rules_text(
                speaker=speaker, members=members, player_role=player_role, group_name=group_name
            ),
        }
    )

    character = compose_character_prompt(character_prompt, immersive)
    if character:
        layers.append({"role": "system", "content": character})

    # 人情账/前情提要/永久记忆：与单聊共用同一段实现，免得两边口径漂移
    layers.extend(continuity_layers(bond=bond, summary=summary, memory=memory))

    if transcript.strip():
        layers.append({"role": "system", "content": "【群聊记录 · 从早到晚】\n" + transcript.strip()})
    if context.strip():
        layers.append({"role": "system", "content": wrap_context(context)})
    # 续接指令（smart 判 END 时才出现）：只说"接着聊"，不改人设、不改记录
    if nudge.strip():
        layers.append({"role": "system", "content": nudge.strip()})

    return layers


JUDGE_SYSTEM = """你是一个多人聊天群的「主持人」，负责决定下一条消息该由谁发出。

【判断依据】
1. 谁最有可能对最近的消息有兴趣、有信息、有立场 —— 优先让他说。
2. 刚发过言的人短期不要连续发言（除非被点名或被直接反驳）。
3. 不要机械轮转。真实群聊里有人话多有人话少。
4. 玩家刚发言后应该有角色接话，不要冷场。
5. 如果最近几条在反复追问同一个问题、或话题已经聊完 —— 让对话自然停下来。
6. 某个角色被点名（「@某某」「某某你怎么看」）时优先让他回答。

【严格输出 JSON，不要任何多余文字】
{"next": "角色名", "reason": "一句话理由"}

- next 只能是成员列表里的名字；若应当停止发言，next 填 "END"。
"""


__all__ = [
    "GROUP_RULES",
    "JUDGE_SYSTEM",
    "build_group_messages",
    "build_transcript",
    "group_rules_text",
]
