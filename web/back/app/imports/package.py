"""imports/package.py —— 拆开老客户端 / 老网页导出的数据包。

老客户端（APK/EXE）和网页版的「导出数据」产出两种文件，这一层都吃：

    BAKER-20261115-2130.zip
        project.json                        版本、我的性别、设置快照、群聊卡片元数据、列表顺序
        cards/00/conversations/00-伊冯.json  每段对话（messages + contextHistory）
        cards/00/images/00-3.png            图片消息，JSON 里只留 ZIP 内路径
        prompts/characters/伊冯.txt          用户自己改过的角色提示词（仅自定义时有）

    BAKER-20261115-2130.json
        单文件，图片以 dataURL 内联在消息里（旧版兼容格式）
        也兼容更早的三种形状：顶层裸数组 / characterCards / characters

**这一层只负责"把字节变成 Python 对象"**：认路径、读文本、校验版本。
什么字段叫什么、群聊怎么认、图片怎么落盘，全在 normalize.py / apply.py，
这样拆开读的时候不用同时想两件事。

`read_member` 是给 ZIP 用的"按路径取文件"回调（图片在被用到的那一刻才读）；
JSON 包没有独立文件，它是个恒返回 None 的空实现。
"""

import contextlib
import io
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ZIP 包里约定的目录结构（和客户端 utils/zipExport.ts 完全一致，改这里就得同步改那边）
_FILE_PROJECT = "project.json"
_EXT_PROMPT = ".txt"
_DIR_PROMPTS = "prompts/characters/"
# cards/<下标>/conversations/<下标>-<角色名>.json —— 两段下标都要能吃 0、00、000
_CARD_CONVERSATION_RE = re.compile(r"^cards/(\d+)/conversations/[^/]+\.json$")

# 客户端只在版本 1 上做导出；收到别的版本说明结构可能变了，宁可不导也不猜
_SUPPORTED_VERSIONS = {1}


class PackageError(Exception):
    """包打不开/结构不对。调用方转成 400，带上这句话给用户看。"""


@dataclass(slots=True)
class RawPackage:
    """一个拆开的包。`cards` 是**未归一化**的原始字典，字段名还是老包里的那套。"""

    kind: str
    version: int
    root: dict[str, Any]
    cards: list[dict[str, Any]]
    prompts: dict[str, str]
    order: list[str]
    read_member: Callable[[str], bytes | None]
    close: Callable[[], None]

    def close_quietly(self) -> None:
        """关掉底层 ZIP（JSON 包是空操作）。读图之后一定要调，否则句柄泄漏。"""
        # 关不上不影响已经读出来的数据，不打扰用户
        with contextlib.suppress(OSError):
            self.close()


def open_package(data: bytes, filename: str) -> RawPackage:
    """按内容（不是扩展名）判断是 ZIP 还是 JSON，然后拆开。"""
    if _looks_like_zip(data, filename):
        return _open_zip(data)
    return _open_json(data)


def _looks_like_zip(data: bytes, filename: str) -> bool:
    """先看魔数 `PK\\x03\\x04`，看不出来再看扩展名。

    为什么不只看扩展名：用户从微信/QQ 转存一次，文件名经常变成 `BAKER.zip.zip`
    或者干脆没有扩展名，靠内容判断永远比靠名字判断可靠。
    """
    if data[:4] == b"PK\x03\x04":
        return True
    return filename.lower().endswith(".zip")


# ---------------------------------------------------------------- ZIP
def _open_zip(data: bytes) -> RawPackage:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageError("这个文件不是有效的 ZIP 压缩包（可能下载/传输时损坏了）") from exc

    try:
        root = _read_project_json(archive)
        cards = _read_zip_cards(archive, root)
        prompts = _read_zip_prompts(archive)
    except PackageError:
        archive.close()
        raise
    if not cards:
        archive.close()
        raise PackageError("这个包里没有任何对话数据（cards 目录是空的）")

    return RawPackage(
        kind="zip",
        version=_version_of(root),
        root=root,
        cards=cards,
        prompts=prompts,
        order=_order_of(root),
        read_member=_member_reader(archive),
        close=archive.close,
    )


def _read_project_json(archive: zipfile.ZipFile) -> dict[str, Any]:
    """读 project.json 并校验版本。缺这个文件说明不是我们导出的包。"""
    try:
        raw = archive.read(_FILE_PROJECT)
    except KeyError as exc:
        raise PackageError("包里缺少 project.json，这不像是本应用导出的数据") from exc
    try:
        root = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError("project.json 解析失败，文件可能已损坏") from exc
    if not isinstance(root, dict):
        raise PackageError("project.json 的内容不是一个对象")

    version = _version_of(root)
    if version not in _SUPPORTED_VERSIONS:
        raise PackageError(
            f"包版本是 {version}，当前后端只认版本 {'/'.join(map(str, _SUPPORTED_VERSIONS))}"
        )
    return root


def _read_zip_cards(archive: zipfile.ZipFile, root: dict[str, Any]) -> list[dict[str, Any]]:
    """按 `cards/00`、`cards/01`… 的顺序读卡片。

    群聊的成员/我的身份/发言模式是**卡片级**字段，存在 project.json 的 cards[]
    里，下标与 cards/NN 严格对齐 —— 这里按同一个下标记拼回去，群聊才不会被
    导成单聊壳（客户端踩过这个坑，见 utils/groupMeta）。

    下标是从文件列表里解析出来的，不是"0 开始试探直到找不到"：后者在 100 张卡
    以上会失效（目录名变成 cards/100，两位补零的假设就不成立了）。
    """
    metas = root.get("cards")
    meta_list = metas if isinstance(metas, list) else []

    paths_by_index: dict[int, list[str]] = {}
    for name in archive.namelist():
        match = _CARD_CONVERSATION_RE.match(name)
        if match is not None:
            paths_by_index.setdefault(int(match.group(1)), []).append(name)

    cards: list[dict[str, Any]] = []
    for index in sorted(paths_by_index):
        paths = sorted(paths_by_index[index])
        conversations = [_read_conversation(archive, path) for path in paths]
        card: dict[str, Any] = {"conversations": conversations}
        meta = meta_list[index] if index < len(meta_list) else None
        if isinstance(meta, dict):
            card.update(meta)
        cards.append(card)
    return cards


def _read_conversation(archive: zipfile.ZipFile, path: str) -> dict[str, Any]:
    try:
        raw = archive.read(path)
        value = json.loads(raw.decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"对话文件读取失败：{path}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"对话文件内容不是一个对象：{path}")
    return value


def _read_zip_prompts(archive: zipfile.ZipFile) -> dict[str, str]:
    """读用户自定义的角色提示词。文件名就是角色名（伊冯.txt → 伊冯）。"""
    prompts: dict[str, str] = {}
    for name in archive.namelist():
        if not (name.startswith(_DIR_PROMPTS) and name.endswith(_EXT_PROMPT)):
            continue
        character_name = name[len(_DIR_PROMPTS) : -len(_EXT_PROMPT)]
        if not character_name:
            continue
        try:
            body = archive.read(name).decode("utf-8-sig")
        except (KeyError, UnicodeDecodeError):
            continue
        if body.strip():
            prompts[character_name] = body
    return prompts


def _member_reader(archive: zipfile.ZipFile) -> Callable[[str], bytes | None]:
    """给上层一个"按 ZIP 内路径取字节"的函数。路径不存在返回 None（不抛错）。"""

    def read(name: str) -> bytes | None:
        try:
            return archive.read(name)
        except KeyError:
            return None

    return read


# ---------------------------------------------------------------- JSON
def _open_json(data: bytes) -> RawPackage:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackageError("文件不是 UTF-8 编码的文本，无法当作 JSON 读取") from exc
    try:
        root: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackageError("JSON 解析失败，文件可能已损坏") from exc

    # 最老的格式是"顶层直接一个卡片数组"，包一层当正常结构处理
    if isinstance(root, list):
        root = {"cards": root}
    if not isinstance(root, dict):
        raise PackageError("无法识别的数据格式：最外层既不是对象也不是数组")

    cards = _json_cards(root)
    if not cards:
        raise PackageError("这个文件里没有找到任何对话数据")
    return RawPackage(
        kind="json",
        version=_version_of(root),
        root=root,
        cards=cards,
        prompts=_json_prompts(root),
        order=_order_of(root),
        read_member=lambda _name: None,
        close=lambda: None,
    )


def _json_cards(root: dict[str, Any]) -> list[dict[str, Any]]:
    """卡片数组可能在三个名字下（新版 cards / 中期 characterCards / 最早 characters）。"""
    for key in ("cards", "characterCards", "characters"):
        value = root.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _json_prompts(root: dict[str, Any]) -> dict[str, str]:
    """自定义提示词：新版叫 promptOverrides，旧版叫 prompts。"""
    raw = root.get("promptOverrides") or root.get("prompts")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): body
        for name, body in raw.items()
        if isinstance(body, str) and body.strip()
    }


# ---------------------------------------------------------------- 公共小工具
def _version_of(root: dict[str, Any]) -> int:
    value = root.get("version")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _order_of(root: dict[str, Any]) -> list[str]:
    """v7 的列表顺序（`s:伊冯` / `g:伊冯|佩丽卡|管理员`）。旧包没有这个字段。"""
    value = root.get("order")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


__all__ = ["PackageError", "RawPackage", "open_package"]
