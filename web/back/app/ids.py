"""ids.py —— 对外 id 与 token 的生成、哈希。

三条规矩：
  1. 对外只给 public_id（32 位 hex），自增 id 永远不出服务端。防的是"遍历 id 把别人的数据翻出来"。
  2. token 用 `secrets` 不用 `random`：random 是可预测的伪随机，拿来发凭证等于没锁门。
  3. token 在库里只存 sha256。库被人拖走，也换不到能用的 token。
"""

import hashlib
import secrets
import uuid

# token 长度：32 字节 → 64 位 hex。够长，肉眼也能看出是凭证。
TOKEN_BYTES = 32


def new_public_id() -> str:
    """生成对外 id（32 位 hex）。用 uuid4 是因为它不泄露"这是第几个"这种信息。"""
    return uuid.uuid4().hex


def new_token() -> str:
    """生成设备 token（64 位 hex）。只有注册那一次会返回明文。"""
    return secrets.token_hex(TOKEN_BYTES)


def sha256_hex(text: str) -> str:
    """把 token 之类的敏感串哈希成 64 位 hex，存库用这个结果。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
