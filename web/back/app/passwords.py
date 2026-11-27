"""passwords.py —— 密码哈希。只用标准库，不引第三方。

为什么不用 bcrypt/argon2：
  这个项目的依赖以 `requirements.lock` 为准，且**本机与服务器必须逐条一致**
  （见设计定稿 §2）。多一个编译型依赖，就多一处"服务器上装不上"的风险；
  而 `hashlib.scrypt` 是标准库自带、底层是 OpenSSL 的实现，强度和 bcrypt 同档。
  所以：能不进依赖就不进依赖。

存进库的格式（自描述，换算法不用改表）：

    scrypt$16384$8$1$<salt base64>$<hash base64>
    pbkdf2$600000$<salt base64>$<hash base64>      ← 只有 scrypt 不可用时才会出现

两条细节值得记下来：

1. **哈希前先做 NFKC 归一化**。中文输入法打出来的全角字符（ｐａｓｓ）和半角
   （pass）在用户眼里是"一样"的，但在字节层面完全不同；不归一化就会出现
   "我明明输对了却登不上"。
2. **校验用 `hmac.compare_digest`**，不用 `==`。`==` 会在第一个不同字节就返回，
   用响应时间能一点点试出哈希 —— 这叫时序攻击，代价只是换个函数。
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import unicodedata

# scrypt 参数：N=16384/r=8/p=1 是"手机时代"的通行值（约 16MB 内存、几十毫秒）
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
# pbkdf2 兜底：OWASP 2024 建议 sha256 至少 60 万次
PBKDF2_ITERATIONS = 600_000
KEY_BYTES = 64
SALT_BYTES = 16

SCHEME_SCRYPT = "scrypt"
SCHEME_PBKDF2 = "pbkdf2"

# 密码长度下限/上限。上限 128 是为了防止"拿 10MB 的请求体做哈希"这种消耗
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def hash_password(raw: str) -> str:
    """把明文密码变成可入库的字符串。每次调用都用新的随机盐。"""
    salt = secrets.token_bytes(SALT_BYTES)
    try:
        digest = _scrypt(raw, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
        head = f"{SCHEME_SCRYPT}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
    except (ValueError, AttributeError):
        # 某些 OpenSSL 构建不带 scrypt（或内存上限过低）——降级而不是崩
        digest = hashlib.pbkdf2_hmac(
            "sha256", _normalize(raw), salt, PBKDF2_ITERATIONS, dklen=KEY_BYTES
        )
        head = f"{SCHEME_PBKDF2}${PBKDF2_ITERATIONS}"
    return f"{head}${_b64(salt)}${_b64(digest)}"


def verify_password(raw: str, stored: str | None) -> bool:
    """校验明文密码。任何异常（存的格式坏了、字段是空的）都当"不对"。"""
    if not stored:
        return False
    parts = stored.split("$")
    try:
        if parts[0] == SCHEME_SCRYPT:
            return _verify_scrypt(raw, parts)
        if parts[0] == SCHEME_PBKDF2:
            return _verify_pbkdf2(raw, parts)
    except (ValueError, TypeError, binascii.Error):
        # 库里的哈希被人手工改坏过：当作密码不对，不要把 500 抛给用户
        return False
    return False


def is_acceptable(raw: str) -> bool:
    """密码强度只要求长度。不搞"必须大小写+符号"——那种规则最后只会让用户
    把密码写在备忘录里，安全性反而更低。"""
    return MIN_PASSWORD_LENGTH <= len(raw) <= MAX_PASSWORD_LENGTH


# ---------------------------------------------------------------- 内部
def _normalize(raw: str) -> bytes:
    """NFKC 归一化 + UTF-8。见模块文档第 1 条。"""
    return unicodedata.normalize("NFKC", raw).encode("utf-8")


def _scrypt(raw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(_normalize(raw), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES)


def _verify_scrypt(raw: str, parts: list[str]) -> bool:
    """`scrypt$N$R$P$salt$hash` —— 六段。"""
    if len(parts) != 6:
        return False
    _scheme, n, r, p, salt_b64, hash_b64 = parts
    digest = _scrypt(raw, _unb64(salt_b64), int(n), int(r), int(p))
    return _compare(digest, _unb64(hash_b64))


def _verify_pbkdf2(raw: str, parts: list[str]) -> bool:
    """`pbkdf2$ITER$salt$hash` —— 四段。"""
    if len(parts) != 4:
        return False
    _scheme, iterations, salt_b64, hash_b64 = parts
    digest = hashlib.pbkdf2_hmac(
        "sha256", _normalize(raw), _unb64(salt_b64), int(iterations), dklen=KEY_BYTES
    )
    return _compare(digest, _unb64(hash_b64))


def _compare(left: bytes, right: bytes) -> bool:
    """定时安全比较（见模块文档第 2 条）。"""
    return hmac.compare_digest(left, right)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "hash_password",
    "is_acceptable",
    "verify_password",
]
