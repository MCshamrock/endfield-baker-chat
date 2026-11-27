"""crypto.py —— 上游 key 的落库加密（AES-256-GCM）。

为什么必须加密：`api_keys.key_enc` 存的是能直接花钱的凭证。
数据库一旦被打包、备份、误传，明文 key 就等于现金。

做法：
  * 密钥来自环境变量 `BAKER_SECRET`（不写进代码、不写进库）；
  * 用 sha256 把它派生成 32 字节（AES-256 要恰好 32 字节）；
  * 每次加密生成随机 nonce，密文 = nonce(12) + 密文 + tag，一起存进 `key_enc`；
  * 解密失败（换了 SECRET）直接抛错，不返回空串 —— 静默失败会让人以为"key 没配置"。

⚠ 换 SECRET = 已存的 key 全部解不开。换之前要用旧 SECRET 解出来再重新加密。
"""

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


def _derive(secret: str) -> bytes:
    """把任意长度的 SECRET 派生成 32 字节密钥。加前缀是为了不与别处的用法撞。"""
    if not secret:
        raise ValueError("BAKER_SECRET 为空，无法加解密上游 key")
    return hashlib.sha256(f"bakerai-v4|{secret}".encode()).digest()


def encrypt(plaintext: str, secret: str) -> bytes:
    """明文 → 可入库的密文。"""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_derive(secret)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt(blob: bytes, secret: str) -> str:
    """密文 → 明文。密文太短或校验失败都会抛错（不吞异常）。"""
    if len(blob) <= NONCE_BYTES:
        raise ValueError("密文长度不对，可能不是本程序写的")
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(_derive(secret)).decrypt(nonce, ciphertext, None).decode("utf-8")


__all__ = ["decrypt", "encrypt"]
