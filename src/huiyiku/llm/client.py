# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class ChatError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def estimate_tokens(text: str) -> int:
    """Rough CJK-aware estimate: ~1.5 chars/token for mixed Chinese."""
    if not text:
        return 0
    return max(1, int(len(text) / 1.5))


def completions_url(endpoint: str) -> str:
    base = (endpoint or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


class ChatClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 120.0,
        session_id: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.session_id = session_id
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # 部分网关（如 opencode zen）要求 x-opencode-session 做路由；
        # 标准端点会忽略未知头，带上无副作用
        if self.session_id:
            headers["x-opencode-session"] = self.session_id
        return headers

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 长响应必须流式：网关/代理对非流式长响应有 ~100 秒空闲上限，
        # 到点直接掐连接（"Server disconnected"/读超时）。流式持续有字节，不受限。
        if max_tokens >= 2048:
            return self._complete_streaming(payload)
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            try:
                resp = client.post(
                    completions_url(self.endpoint),
                    headers=self._headers(),
                    json=payload,
                )
            except (httpx.RemoteProtocolError, httpx.ReadTimeout):
                # 短请求也可能被掐：降级流式重试一次
                return self._complete_streaming(payload)
            except httpx.HTTPError as exc:
                raise ChatError(_friendly_transport_error(exc, self.endpoint), retryable=True) from exc
        return _parse_response(resp)

    def _complete_streaming(self, payload: dict[str, Any]) -> dict[str, Any]:
        """流式累积完整响应，规避长非流式响应的连接上限。"""
        body = {**payload, "stream": True}
        content: list[str] = []
        reasoning_chars = 0
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            try:
                with client.stream(
                    "POST",
                    completions_url(self.endpoint),
                    headers=self._headers(),
                    json=body,
                ) as resp:
                    if resp.status_code >= 400:
                        text = resp.read().decode("utf-8", "replace")
                        raise ChatError(
                            _friendly_llm_error(text, resp.status_code),
                            status_code=resp.status_code,
                            retryable=False,
                        )
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("reasoning_content"):
                            reasoning_chars += len(delta["reasoning_content"])
                        piece = delta.get("content")
                        if piece:
                            content.append(piece)
            except ChatError:
                raise
            except httpx.HTTPError as exc:
                raise ChatError(_friendly_transport_error(exc, self.endpoint), retryable=True) from exc
        text = "".join(content)
        if not text:
            raise ChatError(
                "模型只输出了思考过程、未产出正文（推理占用全部 token 预算）。"
                "请提高 max_tokens 或在设置页改用非推理模型。"
                + (f"（已思考约 {reasoning_chars} 字符）" if reasoning_chars else ""),
                retryable=True,
            )
        return {"content": text, "raw": {"streamed": True}}

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            try:
                async with client.stream(
                    "POST",
                    completions_url(self.endpoint),
                    headers=self._headers(),
                    json=payload,
                ) as resp:
                    if resp.status_code in {401, 403}:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise ChatError(
                            _friendly_llm_error(body, resp.status_code),
                            status_code=resp.status_code,
                            retryable=False,
                        )
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise ChatError(
                            _friendly_llm_error(body, resp.status_code),
                            status_code=resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        token = _parse_sse_line(line)
                        if token is None:
                            continue
                        if token == "[DONE]":
                            break
                        yield token
            except ChatError:
                raise
            except httpx.HTTPError as exc:
                raise ChatError(_friendly_transport_error(exc, self.endpoint), retryable=True) from exc


def _friendly_transport_error(exc: httpx.HTTPError, endpoint: str) -> str:
    """把连接层异常（非 HTTP 状态码）翻译成可操作的中文提示。"""
    if isinstance(exc, httpx.ConnectTimeout | httpx.ConnectError):
        return (
            f"无法连接 {endpoint}：请确认 Base URL 是否写对，以及本机网络/代理能否访问该地址。"
        )
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return f"连接 {endpoint} 后等待响应超时：端点可能过慢、或代理限制了长连接。"
    if isinstance(exc, httpx.RemoteProtocolError):
        return (
            f"与 {endpoint} 的连接在传输中被中断：多为代理/网关对请求做了限制，"
            "可稍后重试或更换端点。"
        )
    return f"访问 {endpoint} 失败：{exc}"


def _friendly_llm_error(body: str, status_code: int) -> str:
    """把网关/端点的常见不兼容错误翻译成可操作的中文提示。"""
    low = body.lower()
    if "missing_session_id" in low or "x-opencode-session" in low:
        return (
            "该端点是 opencode 专有网关，需要 x-opencode-session 专用头，"
            "本应用（标准 OpenAI 兼容客户端）无法使用。请到「设置 → 远程 LLM」"
            "换成直连端点（如 https://api.deepseek.com）。"
        )
    if "invalid_api_key" in low or status_code == 401:
        return "API Key 无效或已过期，请到「设置 → 远程 LLM」重新填写。"
    if "insufficient" in low and ("balance" in low or "quota" in low):
        return "账户余额不足，请到服务商充值后重试。"
    return body


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code in {401, 403}:
        raise ChatError(
            _friendly_llm_error(resp.text, resp.status_code),
            status_code=resp.status_code,
            retryable=False,
        )
    if resp.status_code >= 400:
        raise ChatError(
            _friendly_llm_error(resp.text, resp.status_code),
            status_code=resp.status_code,
            retryable=False,
        )
    try:
        data = resp.json()
    except ValueError as exc:
        # 网关有时返回 200 但正文为空（连接被代理掐断/仅返回表头）：
        # 不能把 JSONDecodeError 抛给上层，否则探测/调用会变成 500。
        snippet = resp.text.strip()[:200] or "（空响应体）"
        raise ChatError(
            f"服务商返回了无法解析的响应（{snippet}）。请确认该端点提供 "
            "OpenAI-compatible Chat Completions，并检查网络/代理是否截断了响应。",
            status_code=resp.status_code,
            retryable=True,
        ) from exc
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    return {"content": content, "raw": data, "usage": data.get("usage") or {}}


def _parse_sse_line(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return "[DONE]"
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
    return delta.get("content") or None


def map_reduce_groups(items: list[str], context_length: int) -> list[list[str]]:
    """Group items so each group is ~60% of context_length tokens."""
    budget = max(512, int(context_length * 0.6))
    groups: list[list[str]] = []
    current: list[str] = []
    used = 0
    for item in items:
        cost = estimate_tokens(item)
        if current and used + cost > budget:
            groups.append(current)
            current = []
            used = 0
        current.append(item)
        used += cost
    if current:
        groups.append(current)
    return groups
