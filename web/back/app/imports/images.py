"""imports/images.py —— 把包里的图片引用变成能落盘的字节。

老包里的图片有两种形态，这里都认：

  * JSON 包 / 未提取的旧格式 → 内联 dataURL：`data:image/png;base64,iVBOR...`
  * ZIP 包 → 包内路径：`cards/00/images/00-3.png`（要配合 `read_member` 去读）

一条硬规矩：**认不出来的图就返回 None，让上层丢掉并记账**。
宁可少一张图，也不要往盘上写一个 `.bin` 的坏文件 —— 那种文件在客户端显示成裂图，
用户会以为整个导入坏了。
"""

import base64
from collections.abc import Callable
from dataclasses import dataclass

# 单张图上限。老包单张通常 ≤1.5MB（导出时会压），但平时导出是原图，
# 所以给到 20MB —— 超过这个数说明不是正常聊天图片，不值得占盘。
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 只认这四种：后端上传接口本来也只收这四种（见 api/attachments.py 的 ALLOWED_MIME）
MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


@dataclass(slots=True)
class RawImage:
    """一张解开来的图。`mime` 一定是上面四种之一，否则上层拿不到 RawImage。"""

    data: bytes
    mime: str


def decode(ref: str, read_member: Callable[[str], bytes | None]) -> RawImage | None:
    """引用 → 图片字节。任何一步不对都返回 None（调用方记账后继续导）。"""
    if not ref:
        return None
    if ref.startswith("data:"):
        return _from_data_url(ref)
    return _from_member(ref, read_member)


def _from_data_url(ref: str) -> RawImage | None:
    """解析 `data:<mime>;base64,<payload>`。非 base64 的 dataURL 直接放弃。"""
    header, _, payload = ref.partition(",")
    if not payload or ";base64" not in header:
        return None
    mime = header[len("data:") :].split(";")[0].strip().lower()
    if mime not in MIME_BY_EXT.values():
        return None
    try:
        data = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return None
    return _checked(data, mime)


def _from_member(ref: str, read_member: Callable[[str], bytes | None]) -> RawImage | None:
    """从 ZIP 里按路径取。扩展名决定 mime —— 客户端写进包的就只有这四种。"""
    extension = ref.rsplit(".", 1)[-1].lower() if "." in ref else ""
    mime = MIME_BY_EXT.get(extension)
    if mime is None:
        return None
    data = read_member(ref)
    if not data:
        return None
    return _checked(data, mime)


def _checked(data: bytes, mime: str) -> RawImage | None:
    """最后一道闸：空文件、超限的都不要。"""
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    return RawImage(data=data, mime=mime)


__all__ = ["MAX_IMAGE_BYTES", "RawImage", "decode"]
