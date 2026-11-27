"""ratelimit.py —— 两道闸门：一个人发多快、全站同时算几个。

为什么只有这两个、不做更复杂的：
  峰值 2.4 请求/分钟。真正的风险不是"量大事多"，而是**单个设备拿脚本狂刷**
  和**某一刻同时挂了几十个生成**（每个生成要等上游几秒到几十秒）。
  所以需要的是"每人每分钟几次"和"全站同时几个"，不是分布式限流中间件。

两个都是**进程内固定窗口**，重启清零：
  * 单进程部署（`workers=1`），不存在多实例口径不一致的问题；
  * 重启清零 = 给所有人一次新配额，代价可接受；
  * 换来的是零依赖、零 Redis、逻辑一眼看得完。

固定窗口的已知缺点：窗口边界处可能放过 2 倍流量（最后一秒 + 第一秒）。
对我们这个量级无所谓；真要精确就得上滑动窗口/令牌桶，那是另一种复杂度。
"""

import time


class FixedWindow:
    """固定窗口计数：每 `window_seconds` 秒允许 `limit` 次。"""

    __slots__ = ("limit", "window", "_buckets")

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, limit)
        self.window = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """允许则计数 +1；否则返回 (False, 还要等几秒)。"""
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))

        if now - start >= self.window:
            # 窗口过期，重新开始
            self._buckets[key] = (now, 1)
            return True, 0.0

        if count >= self.limit:
            return False, max(0.0, self.window - (now - start))

        self._buckets[key] = (start, count + 1)
        return True, 0.0

    def sweep(self) -> None:
        """清掉过期窗口（没人调用也不会错，只是字典会涨）。"""
        now = time.monotonic()
        expired = [k for k, (start, _n) in self._buckets.items() if now - start >= self.window]
        for key in expired:
            self._buckets.pop(key, None)

    def clear(self, key: str) -> None:
        """把这个 key 的计数清掉。

        用于"试对了就归零"：登录成功后清掉该邮箱的失败计数，
        否则白天输错两次、晚上再输错一次，会被当成"连续错三次"。
        """
        self._buckets.pop(key, None)


# ---------------------------------------------------------------- 全局实例
_user_messages: FixedWindow | None = None
_login_by_device: FixedWindow | None = None
_login_by_account: FixedWindow | None = None
_uploads_by_ip: FixedWindow | None = None
_legacy_uploads_by_ip: FixedWindow | None = None
_fetch_by_ip: FixedWindow | None = None


def user_messages() -> FixedWindow:
    """每设备每分钟能发几条（只算真正要出话的那种）。"""
    global _user_messages
    if _user_messages is None:
        from app.settings import get_settings

        _user_messages = FixedWindow(get_settings().user_rpm)
    return _user_messages


def login_by_device() -> FixedWindow:
    """每台设备每分钟能试几次密码。防"拿一台机器狂试"（最简单的爆破）。"""
    global _login_by_device
    if _login_by_device is None:
        from app.settings import get_settings

        _login_by_device = FixedWindow(get_settings().login_rpm)
    return _login_by_device


def login_by_account() -> FixedWindow:
    """每个账号 15 分钟内能试几次。

    为什么还要按账号卡：设备可以换（重装就换一台），账号换不了。
    两道闸门是乘法关系：换设备能绕过第一道，但第二道还拦着。
    """
    global _login_by_account
    if _login_by_account is None:
        from app.settings import get_settings

        settings = get_settings()
        _login_by_account = FixedWindow(
            settings.login_account_rpm, settings.login_account_window_seconds
        )
    return _login_by_account


def uploads_by_ip() -> FixedWindow:
    """每个 IP 每小时能传几个取件包。

    为什么按 IP：上传是免登录的，没有设备/账号可认；IP 是这一层唯一有的东西。
    为什么按小时（3600 秒）：一个包几十兆，正常人一次只传一个。按分钟限额
    挡不住"每分钟一个、传一整天"，而磁盘会不会被写满看的就是小时级的量。
    """
    global _uploads_by_ip
    if _uploads_by_ip is None:
        from app.settings import get_settings

        _uploads_by_ip = FixedWindow(get_settings().upload_rpm, 3600.0)
    return _uploads_by_ip


def legacy_uploads_by_ip() -> FixedWindow:
    """老客户端的上传桶：比新接口宽 6 倍（默认 30 个/小时）。

    为什么不和新接口共用一个桶：老客户端会重试、也会一次补传好几份；
    共用桶等于让老用户被新接口的额度误伤。两边分开，各自的额度各自算。
    """
    global _legacy_uploads_by_ip
    if _legacy_uploads_by_ip is None:
        from app.settings import get_settings

        _legacy_uploads_by_ip = FixedWindow(get_settings().legacy_upload_rpm, 3600.0)
    return _legacy_uploads_by_ip


def fetch_by_ip() -> FixedWindow:
    """每个 IP 每分钟能取几次包（老客户端 /fetch）。

    **和线上老服务同值同窗口**：30 次 / 60 秒。老客户端 429 的文案是"稍等一分钟"，
    窗口做成 60 秒才和它对得上；按小时算（30 次/小时）反而比线上严 60 倍，
    两个用户共用出口 IP 时会互相误伤。
    """
    global _fetch_by_ip
    if _fetch_by_ip is None:
        from app.settings import get_settings

        _fetch_by_ip = FixedWindow(get_settings().fetch_rpm, 60.0)
    return _fetch_by_ip


def reset() -> None:
    """测试用：清掉计数，避免用例之间互相影响。"""
    global _user_messages, _login_by_device, _login_by_account
    global _uploads_by_ip, _legacy_uploads_by_ip, _fetch_by_ip
    _user_messages = None
    _login_by_device = None
    _login_by_account = None
    _uploads_by_ip = None
    _legacy_uploads_by_ip = None
    _fetch_by_ip = None


__all__ = [
    "FixedWindow",
    "fetch_by_ip",
    "legacy_uploads_by_ip",
    "login_by_account",
    "login_by_device",
    "reset",
    "uploads_by_ip",
    "user_messages",
]
