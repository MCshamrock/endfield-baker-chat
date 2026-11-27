"""settings.py —— 配置：环境变量 → 强类型对象。

为什么单独一个文件、还带 pydantic：
  配置项散落在各处 `os.environ.get(...)` 时，最典型的故障是"代码里拼错一个环境变量名，
  线上静默用了默认值，谁都不知道"。这里把名字写死、类型写明，启动时就校验，
  缺项或者类型不对会当场报错，而不是跑到第三条消息才炸。

命名约定：
  环境变量统一 `BAKER_` 前缀（`BAKER_DATABASE_URL`），这里字段就不带前缀。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部运行时配置。改值只改 .env，不改代码。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BAKER_",
        extra="ignore",
    )

    # ---- 数据库 ----
    # 注意 mysql+pymysql 是"同步驱动"。异步驱动（aiomysql）会引入另一套坑，第一版不碰。
    database_url: str = (
        "mysql+pymysql://baker:baker_local_2026@127.0.0.1:3307/baker_back?charset=utf8mb4"
    )

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 5660
    log_level: str = "INFO"

    # ---- 契约 ----
    # 客户端拿这三个数判断"我还需不需要升级、服务端支持什么"。
    api_version: str = "v1"
    schema_version: str = "1"
    app_min: str = "3.0.0"
    # lite = 精简版（只聊天）；full = 完整版。两条产品线共用同一个后端，靠 caps 区分。
    product: str = "lite"

    # ---- 出话：走哪条路 ----
    # inline  = 自己拼提示词、自己调上游（默认，目标形态）
    # gateway = 转给老后端 /chat（过渡/对拍用，老网关一旦下线这个模式就作废）
    gen_mode: str = "inline"

    # ---- 上游（OpenAI 兼容）----
    upstream_base_url: str = "https://apihub.agnes-ai.com/v1"
    upstream_model: str = "agnes-2.5-flash"
    # 单 key 兜底：数据库里没有 key 时用它（本地调试用）。真出话优先走 api_keys 表。
    upstream_api_key: str = ""
    upstream_timeout: float = 60.0
    upstream_max_tokens: int = 2048
    # 2026-11-22 站长定：全部模型输入用 temp=1.3 / top_p=0.7（见下面 top_p 的注释）。
    upstream_temperature: float = 1.3
    # nucleus 采样：只在这个概率质量里挑词。配 0.7 是把长尾砍掉，
    # 免得 1.3 的温度把角色说得太飘。设 0（或 null）= 不发这个字段，用上游默认。
    upstream_top_p: float = 0.7
    # 老后端每轮都带 chat_template_kwargs.enable_thinking；这里保持一致，默认关
    upstream_think_default: bool = False

    # ---- 老客户端兼容：把「同一句再发一遍」当成重新生成 ----
    # 4.0 之前的客户端点「重新生成」＝把玩家那句话再发一遍，服务端看不出区别。
    # 正文与最后一条玩家消息一字不差、且那条是这么多秒内发的 → 按重生成处理
    # （清旧回复、就地重写、不新增玩家消息）。0 = 关掉这条兼容。
    # 为什么给 300 秒：人点重生成都在读完回话后立刻点；隔了几分钟又发同一句，
    # 更可能是真的又说了一遍，那就照常当两条新消息。
    regenerate_echo_seconds: int = 300

    # ---- key 池 ----
    # 加密密钥：api_keys.key_enc 用它加的密。**换了它，已存的 key 全部解不开。**
    secret: str = "dev-only-change-me"
    key_rpm_default: int = 10          # 每个 key 每分钟可用次数（令牌桶容量）
    # 429 之后这把 key 冷却多久（5xx / 网络另有 10 秒的软冷却，见 keys._soft_fail）
    #
    # 为什么是 180 而不是 60：2026-11-18 实测上游的 429 有两种，其中一种是
    # 「今日文本额度已用完，当前处于限速模式：每 3 分钟可请求 1 次」。这种限速
    # 是**按 key**算的，冷却 60 秒根本没撑到下一个窗口，下一分钟又会撞一次 ——
    # 每撞一次就废掉一个用户的请求。按上游自己给的三分钟来冷却，宁可少用一把 key，
    # 也不要反复制造"失败气泡"。（流式路径不绑 key，冷却中的会被跳过选下一把。）
    key_cooldown_seconds: float = 180.0
    key_fail_limit: int = 3            # 连续失败几次直接禁用（等人工处理）

    # ---- 老网关（只给 gen_mode=gateway 用）----
    gateway_url: str = "http://127.0.0.1:5500"

    # ---- 限流 ----
    # 每台设备每分钟最多几次**出话**（只存不发的请求不算）
    user_rpm: int = 12
    # 借不到空闲 key 时最多等多久（不是拒绝人，是让请求排一会儿）
    key_wait_seconds: float = 20.0
    # 登录尝试：每设备每分钟几次、每账号多少秒内几次（防爆破，两道闸门是乘法关系）
    login_rpm: int = 10
    login_account_rpm: int = 20
    login_account_window_seconds: float = 900.0
    # 优先走的站点（逗号分隔）：实测国内站比国际站快一倍，所以优先打国内站，
    # 国内站的 key 用完了/冷却了再落到国际站。
    upstream_prefer_hosts: str = "api.agnes-ai.cn"

    # ---- 世界书检索 ----
    worldbook_enabled: bool = True
    worldbook_slug: str = "endfield"
    # 每轮预取正文的 token 预算（终端同值 2000）
    worldbook_budget_tokens: int = 2000
    # 是否把整份索引（301 条目录 ≈ 2.2k token）每轮塞进 system。
    # 关掉最省，但模型就"不知道有这条设定"；开着贵一点但回答更像这个世界里的人。
    worldbook_index_enabled: bool = True

    # ---- 附件 ----
    # 存哪：默认在项目根的 _runtime/uploads（不进版本库、和数据库文件同一块地方）
    upload_dir: str = str(Path(__file__).resolve().parents[3] / "_runtime" / "uploads")
    # 单张图上限（6 MB）。老客户端把图片 base64 塞在消息体里，这里改成文件上传。
    max_upload_bytes: int = 6 * 1024 * 1024

    # ---- 数据包导入（老客户端 / 老网页「导出数据」搬进来）----
    # 老客户端导出侧的硬上限是 60 MB，这里留一点余量。
    import_max_bytes: int = 64 * 1024 * 1024
    # 临时目录：上传与解包会产生中间文件，**必须留在项目内**（设计定稿 §0 的硬规矩）。
    # 不设的话 Python 会用到 %TEMP% / /tmp，文件就跑到项目外去了。
    tmp_dir: str = str(Path(__file__).resolve().parents[3] / "_runtime" / "tmp")

    # ---- 取件码上传（免登录先传包，之后凭码把数据领进账号）----
    # 码的有效期（小时）。过期后包会被清理任务删掉，用户必须重新上传一次。
    upload_ttl_hours: int = 24
    # 每个客户端 IP 每小时最多传几个包。一个包几十兆，这是"磁盘增长速度"的闸门：
    # 按分钟限额挡不住"每分钟一个、传一整天"。
    upload_rpm: int = 5
    # 老客户端（POST /upload）单独一个更宽的桶。老客户端会重试、也可能一次补传
    # 好几份，用新接口的额度卡它等于让老用户突然传不上去 —— 兼容优先。
    legacy_upload_rpm: int = 30
    # 老客户端取件的频率：**每分钟**几次（和线上一致：30 次/分钟）。
    # 老客户端 429 的文案是"稍等一分钟"，窗口就是 60 秒；按小时算会比线上严 60 倍。
    fetch_rpm: int = 30
    # 全站所有取件包加起来最多占多少字节（2 GB）。到顶就回 507，
    # 让运维跑 scripts/cleanup_uploads.py 腾地方，而不是让磁盘悄悄写满。
    upload_total_bytes: int = 2 * 1024 * 1024 * 1024

    # ---- 上下文预算 ----
    # 单轮提示词总预算（估算 token）。**超预算只压缩，绝不裁切**（裁切会丢记忆）。
    # 上游 512k 上下文；默认按 20 万给，用户可以自己调小。
    prompt_budget_tokens: int = 200_000
    # 给回复留的余量：历史别把预算吃满，否则模型没地方说话
    reply_reserve_tokens: int = 4096

    # ---- 摘要 / 压缩 ----
    # 历史超过「预算 × 这个比例」就开始压；压到「预算 × compress_to_ratio」停。
    # 中间那段差额天然形成滞回，免得每轮都触发压缩。
    compress_at_ratio: float = 0.8
    compress_to_ratio: float = 0.5
    # 单次压缩最多吞多少 token（控制单次摘要调用的成本；不是裁切，剩下的下一轮继续压）
    compress_chunk_tokens: int = 30_000
    # 一次请求里最多压几轮。撞到这个数说明系统出问题了（正常压 1~2 轮就够），
    # 这时**宁可超预算也不裁**，只记错误日志。
    compress_max_rounds: int = 8
    # 从库里一次最多取多少条历史（纯防呆，不是发给模型的上限）
    history_fetch_limit: int = 2000
    # 压缩时保留最近几轮原文（一轮 = 用户 + 角色两条）
    keep_recent_turns: int = 4
    # 摘要本身的输出上限（滚动摘要：每次把「旧提要 + 新一段」重写成一份，长度受这个约束）
    summary_max_tokens: int = 3000
    # 永久记忆：摘要里提炼出的**新增**条目，只追加不改写
    memory_max_tokens: int = 1000
    # 永久记忆长度的提醒线（只警告，不截断 —— 截断就等于删记忆）
    memory_warn_chars: int = 4000

    @property
    def caps(self) -> list[str]:
        """这一版服务端真的支持哪些能力（字符串列表，老客户端只认这个形状）。

        只写**已经能跑**的，不写"计划中"的 —— 客户端是按这个列表决定显示什么的，
        写了没实现的能力，等于骗前端画出点不动的按钮。
        """
        return [
            "conversations",
            "messages",
            "idempotency",
            "speaker",
            "import",
            "login",
            "memory",       # 跨会话干员记忆
            "suggestions",  # 输入推荐
            "group",        # 群聊（含发言权重）
            "continue",     # 继续说
            "think",        # 上游思考过程
            "reasoning",    # 思考过程落库（只有后台能读）
        ]

    @property
    def capabilities(self) -> dict[str, bool]:
        """能力开关（布尔字典，新客户端按它决定显示/隐藏按钮）。

        为什么既要有 `caps` 又要有它：
          * `caps` 是**列表**，老客户端已经在用，形状不能动；
          * 布尔字典能明确表达"不支持"，这是列表做不到的 ——
            列表里没有"web_search"，客户端只能猜是"实现了但没写在列表里"还是"真没有"。

        `web_search` 恒为 False：新后端**没有联网搜索**，检索只走自有世界书。
        这不是没做，是故意的（见 docs/12）。老客户端如果传 `use_rag=false`，
        服务端会记一条 warning，但**不会**假装联网 —— 假装比不支持更糟。
        """
        return {
            "web_search": False,
            "worldbook": True,
            "suggestions": True,
            "group": True,
            "group_weighted": True,
            "continue": True,
            "think": True,
            "reasoning_stored": True,
            "memory": True,
            "import": True,
            "login": True,
            "attachments": True,
            "stream": True,
        }


@lru_cache
def get_settings() -> Settings:
    """全局只读一份配置。lru_cache 保证 import 一次、用到处都一样。"""
    return Settings()
