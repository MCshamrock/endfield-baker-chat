"""schemas.py —— 请求与响应的形状（对外契约就写在这里）。

为什么单独一个文件：
  路由函数应该只表达"这个请求要什么、我做了什么"，字段定义挪到这里之后，
  想看契约就翻这一个文件，不用在 11 个路由里找。

命名约定：
  * 结尾 `Request` 的是**进来的**数据（带校验）
  * 结尾 `Out` 的是**出去的**数据（决定客户端看到什么）
  * 出参里绝不出现数据库自增 id，只出现 public_id / slug / seq

字段名一律 snake_case，不做驼峰转换：客户端都是我们自己写的，多一层转换只是多一个
"哪个字段忘了转"的 bug 来源。
"""

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------- 元信息
class MetaOut(BaseModel):
    """`GET /v1/meta`。客户端靠它判断版本与能力。"""

    api: str
    schema_: str = Field(alias="schema")  # schema 是 pydantic 的保留字，字段名加下划线、别名照旧
    app_min: str
    product: str
    caps: list[str]
    # 布尔能力表：列表表达不了"明确不支持"，这一项能（web_search=false 就是没有）
    capabilities: dict[str, bool] = Field(default_factory=dict)
    server_time: str

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------- 设备
class DeviceRegisterRequest(BaseModel):
    """`POST /v1/auth/device` 的请求体。全部可选 —— 客户端什么都不传也能注册。

    这里给的 `max_length` 比数据库列宽（platform/app_ver/os_ver 32、model/tz 64）
    宽松，真正落库前由 `api/auth.py` 按列宽截断。为什么不干脆限到列宽：
    真实客户端的 UA / 安卓版本串经常超过 32 字，直接 422 会让"注册设备"整条挂掉，
    而设备元信息截断没有任何业务影响。
    """

    platform: str = Field(default="unknown", max_length=4096)
    app_ver: str = Field(default="", max_length=4096)
    app_build: int = 0
    model: str = Field(default="", max_length=4096)
    os_ver: str = Field(default="", max_length=4096)
    tz: str = Field(default="", max_length=4096)
    locale: str = Field(default="", max_length=4096)


class DeviceRegisteredOut(BaseModel):
    """注册结果。`token` 只在这里出现一次，客户端必须自己存好。"""

    device_id: str
    token: str


# ---------------------------------------------------------------- 账号（登录）
class RegisterRequest(BaseModel):
    """注册。

    登录标识是**用户名**，不是邮箱：这一版没有发信能力，邮箱的唯一用处
    （验证码 / 找回密码）现在都做不了，让用户填只是多一步。
    `username` 的字符规则由 `api/auth.py` 校验（2–20 字，中文/字母/数字/下划线）。
    """

    username: str = Field(min_length=2, max_length=20)
    password: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    """账号对外形状。`id` 是 public_id；**绝不出现密码哈希**。"""

    id: str
    username: str
    display_name: str
    created_at: str


class DeviceOut(BaseModel):
    """账号名下的一台设备（"我的设备"列表）。"""

    id: str
    platform: str
    app_ver: str
    last_seen_at: str | None = None


class AuthResultOut(BaseModel):
    """登录/注册的结果。

    `adopted_conversations`：这次登录把多少条"登录前在这台设备上聊的记录"
    划到了账号名下。给客户端一句"已把 N 条记录并入账号"的提示用。
    """

    user: UserOut
    adopted_conversations: int = 0


class MeOut(BaseModel):
    """`GET /v1/auth/me`。

    `kind='guest'` 表示还没登录（数据只在当前设备名下）；
    `kind='user'` 时 `user` 一定有值，`devices` 是这个账号登过的设备。
    """

    kind: str
    user: UserOut | None = None
    devices: list[DeviceOut] = Field(default_factory=list)


class OwnerDataOut(BaseModel):
    """`GET /v1/auth/owner-data`：这份身份名下到底有没有聊天记录。

    只回数字，不回内容。客户端启动时用它判断一件事：
    **这个人已经有记录、却没登录账号 → 先把人拦下来登录/注册**。
    因为记录此刻挂在"这台设备"名下，清一次浏览器/换台设备就找不着了。
    """

    conversations: int = 0
    messages: int = 0


# ---------------------------------------------------------------- 角色
class CharacterOut(BaseModel):
    """一个角色。`id` 是 slug（perlica），不是数据库自增 id。"""

    id: str
    name: str
    avatar: str = ""
    attribute: str = ""
    # 群聊里她抢话的倾向（1~100）。**角色默认值**，不随玩家变化。
    # 保留它是为了兼容老客户端（它们只认这个字段）。
    active_weight: int = 50
    # **有效值**：玩家覆盖 > 角色默认。带 token 请求时才是"你自己的值"，
    # 不带 token 就和 active_weight 一样。
    weight: int = 50
    # 有效值从哪来："user" = 玩家改过；"default" = 用角色自带值
    weight_source: str = "default"


class CharacterListOut(BaseModel):
    items: list[CharacterOut]


# ---------------------------------------------------------------- 会话
class MemberWeightOut(BaseModel):
    """群成员的有效发言权重（顺序和 `ConversationOut.members` 一一对应）。

    为什么不把 `members` 直接改成对象数组：`members` 现在是字符串名数组，
    老客户端按名字读、既有测试也断言它是字符串数组。为了加这个信息去改类型，
    等于"为加字段破老形状"。所以另开一个平行字段，老客户端直接忽略它。
    """

    character_id: str  # slug，例如 perlica
    name: str
    weight: int
    weight_source: str  # "user" / "default"


class ConversationCreateRequest(BaseModel):
    """建会话。

    * 单聊：`character_id` 传 slug
    * 群聊：`mode="group"` + `members`（至少两个 slug）；`character_id` 可省略
    """

    character_id: str = Field(default="", max_length=64)
    mode: str = Field(default="single", pattern="^(single|group)$")
    title: str = Field(default="", max_length=120)
    # 群聊专用
    members: list[str] = Field(default_factory=list)
    speak_mode: str = Field(default="weighted", pattern="^(round|smart|assign|weighted)$")
    player_role: str = Field(default="observer", max_length=64)
    # 干员记忆：inherit 跟随全局默认，on/off 单独覆盖这一局
    memory: str = Field(default="inherit", pattern="^(inherit|on|off)$")


class ConversationOut(BaseModel):
    """会话。`id` 是 public_id —— 客户端之后所有请求都用它当指针。"""

    id: str
    character_id: str | None
    mode: str
    title: str
    next_seq: int
    speak_mode: str = "weighted"
    memory_mode: str = "inherit"
    # 群成员中文名（按发言顺序）。**保持字符串数组**，老客户端按名字读。
    members: list[str] = Field(default_factory=list)
    # 每个成员的有效权重，顺序与 members 一一对应。
    # 只有群聊会话详情（GET /v1/conversations/{id}）/ 建群响应会填；单聊恒为 []。
    member_weights: list[MemberWeightOut] = Field(default_factory=list)
    created_at: str
    last_active_at: str


class ConversationListOut(BaseModel):
    items: list[ConversationOut]


# ---------------------------------------------------------------- 消息
class MessageCreateRequest(BaseModel):
    """发一条消息。

    `client_msg_id` 是幂等键：弱网重发同一个 id，服务端只会存一条。
    客户端应该每发一条就生成一个新 id（例如 `c-<时间戳>-<随机数>`），重试时沿用旧的。

    三个开关（默认值都和老客户端一致）：
      * `generate`  是否让服务端接着出话；只存不发就传 false
      * `immersive` 沉浸式：不显示括号里的动作描写（老后端默认就是开）
      * `think`     上游的"思考过程"开关（老后端默认关）
    """

    client_msg_id: str = Field(min_length=1, max_length=64)
    text: str = ""
    speaker: str = Field(default="", max_length=64)
    # 群聊 assign 模式：指定这一次由谁说（传 slug）
    assign_speaker: str = Field(default="", max_length=64)
    # 结构化 @：群里被点名的 slug（前端精确 @ 的产物）。老客户端不传，走文本点名兜底。
    mentions: list[str] = Field(default_factory=list, max_length=8)
    attachment_id: str | None = None
    # 玩家手机上的本地时间（例如「2026-11-16 23:20 周日」）。只进提示词，不入库。
    local_time: str = Field(default="", max_length=40)
    # 「时间感知」开关（默认开 = 老客户端不传时行为不变）。
    # 关掉时**完全不把时间告诉模型**：local_time 被丢弃，提示词里不会出现系统时钟，
    # 角色只能含糊其辞（不是"提醒它别报时"）。
    time_enabled: bool = True
    generate: bool = True
    immersive: bool = True
    think: bool = False
    # 「重新生成」= 把**这一轮的回复**重写一遍，不新增玩家消息（见 api/conversations
    # 的 _regenerate_turn）。老客户端不传，默认 false，行为完全不变。
    regenerate: bool = False


class ContinueRequest(BaseModel):
    """点「继续说」：接着上一条 assistant 往下写，玩家不用再打一句。

    `client_msg_id` 是幂等键 —— 网络重试时服务端不会再生成一段，
    而是把上一次的结果原样回放（否则同一段话后面会多出一整段）。
    """

    client_msg_id: str = Field(min_length=1, max_length=64)
    local_time: str = Field(default="", max_length=40)
    # 与 MessageCreateRequest 同一个开关：续写 / 群聊「下一句」也要尊重它
    time_enabled: bool = True
    think: bool = False
    # 群聊「下一句」专用：指定这一次让谁说（空 = 按发言策略自己选）
    speaker: str = Field(default="", max_length=64)


class AttachmentOut(BaseModel):
    """上传结果。`dedup=true` 表示同内容的图之前已经存过，这次没有新建。"""

    id: str
    mime: str
    bytes: int
    sha256: str
    url: str
    dedup: bool = False


class UsageOut(BaseModel):
    """这一轮花了多少 token。流式回复拿不到真实值，所以为 null。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_ms: int = 0


class MessageCreatedOut(BaseModel):
    """写消息的结果。

    `reply` 是服务端生成的那句话；`generate=false` 或上游失败时为 null。
    `duplicate=true` 表示这条 client_msg_id 之前已经存过，服务端没有新建消息
    （此时也不会再生成回复，直接回放旧的 seq）。
    """

    seq: int
    duplicate: bool
    reply: str | None = None
    reply_seq: int | None = None
    # 群聊里这一句是谁说的（单聊为空）；`reply=null` 且 `silent=true` 表示主持人判定该停
    speaker: str = ""
    silent: bool = False
    usage: UsageOut | None = None
    created_at: str


class ContinueOut(BaseModel):
    """「继续说」的结果。没有用户消息，所以没有 seq，只有 reply_seq。"""

    reply: str
    reply_seq: int
    duplicate: bool = False
    usage: UsageOut | None = None
    created_at: str


class SuggestionsOut(BaseModel):
    """输入推荐：给玩家"接下来可以说什么"的 3 条候选。

    失败时是空列表而不是报错 —— 推荐挂了不该让聊天界面弹错误框。
    """

    suggestions: list[str] = Field(default_factory=list)


class NextTurnOut(BaseModel):
    """群聊「下一句」的结果。

    没有玩家新消息，所以没有 seq；`done=true` 表示本轮所有人都说过了
    （或者主持人判定该停），客户端应停下等待玩家发言。
    """

    reply: str | None = None
    reply_seq: int | None = None
    speaker: str = ""
    done: bool = True
    remaining: list[str] = Field(default_factory=list)
    duplicate: bool = False
    usage: UsageOut | None = None
    created_at: str


class MessageOut(BaseModel):
    """一条历史消息。带图的消息会给出附件 id 与取回地址。

    `attachment_id` 是附件的 public_id，取原图走 `attachment_url`
    （服务端还要校验归属，所以不是裸文件地址）。
    """

    seq: int
    side: str
    speaker: str
    text: str
    attachment_id: str | None = None
    attachment_url: str | None = None
    created_at: str


class MessageListOut(BaseModel):
    """增量拉取。客户端下次带 `after_seq=next_after_seq` 继续拉即可。"""

    items: list[MessageOut]
    next_after_seq: int


# ---------------------------------------------------------------- 数据包导入
class ImportResultOut(BaseModel):
    """一次导入的结果。

    * `duplicate=true` —— 这个包之前导过（逐字节相同），没有再导一遍，数字是上次的
    * `notes` —— 中文说明："哪几个角色没收进来""丢了几条上下文"。空列表 = 一条没丢
    * `revoked_at` 非空 —— 这次导入已经被撤销（它建的会话已软删）
    """

    id: str
    duplicate: bool = False
    source: str
    mode: str
    package_version: int
    cards: int = 0
    conversations: int = 0
    messages: int = 0
    images: int = 0
    skipped: int = 0
    notes: list[str] = Field(default_factory=list)
    created_at: str
    revoked_at: str | None = None


class ImportListOut(BaseModel):
    items: list[ImportResultOut]


# ---------------------------------------------------------------- 干员记忆
class MemoryOut(BaseModel):
    """一条人情账。`character_id` 是 slug。"""

    id: int
    character_id: str
    kind: str
    text: str
    pinned: bool = False
    created_at: str
    updated_at: str


class MemoryListOut(BaseModel):
    character_id: str
    mode: str
    items: list[MemoryOut]
    total: int
    limit: int


class MemoryPatchRequest(BaseModel):
    """改文本 / 置顶。两个都可选，只传一个就是只改那一个。"""

    text: str | None = Field(default=None, max_length=300)
    pinned: bool | None = None


class MemorySettingsOut(BaseModel):
    mode: str


class MemorySettingsRequest(BaseModel):
    mode: str = Field(pattern="^(on|off)$")


class WeightRequest(BaseModel):
    """玩家自定义某个干员的发言权重。"""

    active: int = Field(ge=1, le=100)


class WeightOut(BaseModel):
    character_id: str
    active: int
    source: str  # "user" = 玩家自定义过， "default" = 用角色自带值
