"""errors.py —— 全站只有一种错误出口。

为什么统一：
  路由里到处 `try/except` 各回各的格式，客户端就得为每种错误写一套判断。
  这里规定：业务代码只管抛 `AppError(code, 中文话, http_status)`，
  由 `main.py` 里唯一的异常处理器转成同一种 JSON。

错误体形状（客户端只看这一个）：
    {"error": {"code": "no_conversation", "message": "会话不存在，或者不属于这台设备",
               "request_id": "8c1a..."}}

code 给程序判断（英文、稳定、不改），message 给人看（中文、可以改）。
"""

from typing import Any


class AppError(Exception):
    """业务错误。抛出即终止本次请求，由全局处理器转成 JSON。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        # 需要额外说明时用（例如"哪几个字段不合法"），默认不带
        self.details = details or {}


# ---- 下面这些是"常用的那几个"，写成函数是为了调用处读起来像句子 ----

def no_token() -> AppError:
    return AppError("no_token", "缺少 Authorization: Bearer <token>", http_status=401)


def bad_token() -> AppError:
    return AppError("bad_token", "token 无效或已被停用", http_status=401)


def no_conversation() -> AppError:
    # 故意用 404 而不是 403：403 等于告诉对方"这个会话存在，只是不给你看"，
    # 那正好帮人确认 id 是对的。404 什么都不泄露。
    return AppError("no_conversation", "会话不存在，或者不属于这台设备", http_status=404)


def no_character() -> AppError:
    return AppError("no_character", "没有这个角色，或者它已下架", http_status=404)


def bad_request(message: str) -> AppError:
    return AppError("bad_request", message, http_status=400)


# ---- 账号相关 ----

def bad_credentials() -> AppError:
    """登录失败。

    故意**不区分**"账号不存在"和"密码不对"：分开说等于给了一个"这个用户名有人用没有"
    的探测接口。一句话、一个 401，谁都问不出来。
    """
    return AppError("bad_credentials", "用户名或密码不对", http_status=401)


def weak_password(reason: str) -> AppError:
    return AppError("weak_password", reason, http_status=400)


def username_taken() -> AppError:
    # 409 而不是 400：这是"和已存在的资源冲突"，语义更准。
    # 代价是它能被用来探测"这个用户名有没有人用"—— 免费产品 + 注册本来就
    # 要告诉你这件事，不值得为它牺牲体验。
    return AppError("username_taken", "这个用户名已经有人用了，换一个吧", http_status=409)


def forbidden() -> AppError:
    """没有权限。和 401 分开：401 是"你是谁不知道"，403 是"知道你是谁，但你不能"。"""
    return AppError("forbidden", "需要管理员权限", http_status=403)


def no_user() -> AppError:
    return AppError("no_user", "这个操作需要先登录", http_status=401)


def device_taken() -> AppError:
    return AppError(
        "device_taken",
        "这台设备已经登录了另一个账号，请先退出再登录新的",
        http_status=409,
    )


def too_many_attempts(retry_after: float) -> AppError:
    return AppError(
        "too_many_attempts",
        f"尝试次数太多了，{max(1, round(retry_after))} 秒后再试",
        http_status=429,
        details={"retry_after": round(retry_after, 1)},
    )
