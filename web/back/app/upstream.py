"""upstream.py —— 调上游模型（OpenAI 兼容接口，httpx 异步）。

只做两件事：发一次请求拿整段回复、发一次请求逐段拿流式增量。
提示词怎么拼、回复怎么后处理、用量怎么记账，都不在这个文件里。

为什么用异步：这是全链路里唯一真正"在等网络"的地方（几秒到几十秒）。
同步写的话，一个用户在等回复，其他所有请求都会被堵住 —— 单进程服务的死法就这一种。

接口形状照抄老后端（`common.AgnesClient`）：

    POST {base}/chat/completions
    { "model": ..., "messages": [...], "stream": false,
      "temperature": 0.8, "max_tokens": 2048,
      "chat_template_kwargs": {"enable_thinking": false} }

`chat_template_kwargs.enable_thinking` 是这个上游的私有开关（控制"思考过程"），
OpenAI 标准里没有，但老后端一直在用，去掉会改变模型行为。
"""

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.keys import Credential
from app.settings import get_settings

logger = logging.getLogger("baker.upstream")


class UpstreamError(RuntimeError):
    """上游调用失败。

    带两个字段给 key 池做惩罚判断：
      * `status`：HTTP 状态码（连接失败时为 None）
      * `network`：是否是连接层失败（超时/断线），这种不该重罚 key
    """

    def __init__(self, message: str, *, status: int | None = None, network: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.network = network


@dataclass(slots=True)
class Reply:
    """一次完整回复的结果。"""

    text: str
    # 上游的思考过程（reasoning_content）。**只落库，不下发**，见 docs/08 §1.3
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


def _build_payload(
    credential: Credential,
    messages: list[dict[str, object]],
    *,
    think: bool,
    stream: bool,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
) -> dict[str, object]:
    settings = get_settings()
    payload: dict[str, object] = {
        "model": credential.model or settings.upstream_model,
        "messages": messages,
        "stream": stream,
        "temperature": settings.upstream_temperature if temperature is None else temperature,
        "max_tokens": settings.upstream_max_tokens if max_tokens is None else max_tokens,
        "chat_template_kwargs": {"enable_thinking": think},
    }
    # top_p：默认取 settings（全站统一），调用方也能按次覆盖。
    # 配 0 / None 就不发这个字段 —— 那是"用上游默认"，不是"top_p=0"（那样模型只能从最大概率的那个词里挑）。
    effective_top_p = settings.upstream_top_p if top_p is None else top_p
    if effective_top_p:
        payload["top_p"] = effective_top_p
    return payload


def _headers(credential: Credential) -> dict[str, str]:
    if not credential.key:
        raise UpstreamError("这个 key 是空的，检查 api_keys 表或 BAKER_UPSTREAM_API_KEY")
    return {
        "Authorization": f"Bearer {credential.key}",
        "Content-Type": "application/json",
    }


def _pick_reasoning(data: dict[str, object]) -> str:
    """取思考过程。三种字段名都认（不同上游叫法不同），取不到就是空串。

    取不到很正常：`enable_thinking=false` 时上游根本不返回这个字段。
    """
    try:
        message = data["choices"][0]["message"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return ""
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _pick_usage(data: dict[str, object]) -> tuple[int, int, int]:
    """从 response 里取 (prompt, completion, cached)。

    不同上游把"缓存命中"放在不同位置，这里三个都认，取不到就当 0。
    cached_tokens 是我们判断"key 粘性有没有生效"的唯一依据，能取就一定要取到。
    """
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)

    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    if not cached:
        cached = int(usage.get("cached_tokens") or 0)
    return prompt_tokens, completion_tokens, cached


async def complete(
    messages: list[dict[str, object]],
    *,
    credential: Credential,
    think: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> Reply:
    """非流式：一次拿完整回复。

    `max_tokens` / `temperature` 可覆盖：摘要、起标题这类内部调用要短、要稳，
    跟聊天用的参数不是一回事。
    """
    settings = get_settings()
    url = f"{credential.base_url.rstrip('/')}/chat/completions"
    payload = _build_payload(
        credential, messages, think=think, stream=False,
        max_tokens=max_tokens, temperature=temperature,
    )

    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
            response = await client.post(url, headers=_headers(credential), json=payload)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"连不上上游：{exc}", network=True) from exc

    if response.status_code >= 400:
        raise UpstreamError(_error_text(response), status=response.status_code)

    data = response.json()
    try:
        message = data["choices"][0]["message"]
        text = message.get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise UpstreamError(f"上游返回结构不对：{str(data)[:200]}") from exc
    if not text:
        raise UpstreamError("上游返回了空内容")

    prompt_tokens, completion_tokens, cached = _pick_usage(data)
    return Reply(
        text=text,
        reasoning=_pick_reasoning(data),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached,
    )


async def stream(
    messages: list[dict[str, object]], *, credential: Credential, think: bool = False
) -> AsyncIterator[str]:
    """流式：逐段吐出增量文本。

    只 yield 文本增量。用量（usage）上游一般只在最后一帧给，所以这里不返回，
    调用方可以自己按字数估算，或者改成"再发一次非流式请求取 usage"——但那样多花一次钱，
    所以本篇的用量记账对**流式回复**采用"按字符估"（见 generate.py 的说明）。
    """
    settings = get_settings()
    url = f"{credential.base_url.rstrip('/')}/chat/completions"
    payload = _build_payload(credential, messages, think=think, stream=True)

    try:
        async with (
            httpx.AsyncClient(timeout=settings.upstream_timeout) as client,
            client.stream("POST", url, headers=_headers(credential), json=payload) as response,
        ):
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                # 429 的原始响应体里有"额度/几点后重试"这类关键信息，留一份给运维看；
                # 给用户的那句话由 _error_text 统一整理（不能把一整坨 JSON 甩到气泡里）。
                if response.status_code == 429:
                    logger.warning("上游限速 429：%s", body[:400].replace("\n", " "))
                raise UpstreamError(
                    _error_text(response, body),
                    status=response.status_code,
                )

            async for line in response.aiter_lines():
                delta = _parse_sse_line(line)
                if delta == "[DONE]":
                    break
                if delta:
                    yield delta
    except httpx.HTTPError as exc:
        raise UpstreamError(f"流式连接断了：{exc}", network=True) from exc


def _parse_sse_line(line: str) -> str:
    """解析一行 SSE。

    返回：文本增量 / `[DONE]`（结束）/ 空串（这行不需要处理）。
    """
    raw = line.strip()
    if not raw.startswith("data:"):
        return ""
    chunk = raw[len("data:") :].strip()
    if chunk == "[DONE]":
        return "[DONE]"
    try:
        obj = json.loads(chunk)
    except json.JSONDecodeError:
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _error_text(response: httpx.Response, body: str = "") -> str:
    """把上游的错误整理成一句**能直接给用户看**的话。

    2026-11-18 修：流式分支以前直接把 upstream 的原始 JSON 拼进气泡
    （「上游 HTTP 429: {"error":{"message":"今日文本额度已用完…"}}」），
    既丑又长。现在两条路径共用这一个函数：

      * 429 → 「今日额度已用完，请于 23:05 后再试」；连时间都解析不出来就说「稍等再发」。
        （上游是**按账号**限额的，换 key 没用 —— 这一点在文案里不提，免得用户反复点。）
      * 其它 4xx/5xx → 保留状态码 + 一小段原文（截断），够定位就行。
    """
    if response.status_code == 429:
        clock = _retry_clock(body or response.text)
        if clock:
            return f"上游今日额度已用完，请于 {clock} 后再试"
        return "上游限速（429），请稍等再发"
    text = (body or response.text)[:200].replace("\n", " ")
    return f"上游 HTTP {response.status_code}：{text}"


def _retry_clock(text: str) -> str:
    """从上游的 429 文案里抠出「请于 23:05:11 后重试」那个时刻，返回 "23:05"。

    抠不到就返回空串 —— 调用方会退回一句不带时间的提示。
    只看第一处 HH:MM(:SS)，避免把 request id 里的数字当成时间。
    """
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text or "")
    if not match:
        return ""
    hour = int(match.group(1))
    minute = match.group(2)
    if hour > 23:
        return ""
    return f"{hour:02d}:{minute}"


__all__ = ["Reply", "UpstreamError", "complete", "stream"]
