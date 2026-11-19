"""llm.py — 大模型调用层（OpenAI 兼容协议）

后端按 OpenAI Chat Completions 兼容协议访问大模型：
Base URL、路径、鉴权头、模型名全部走 .env 配置，换供应商只需改配置、不动业务代码。

- 兼容两种返回：标准 JSON、SSE 流式（逐行解析 data: 块）
- 上游返回 {"code": 非 0} 或 HTTP 非 2xx 时抛 LLMError，由调用方决定兜底
- 未配置 base_url / api_key 时 enabled() 为 False，直接走本地确定性算法
"""

import json

import httpx

from ..core.config import settings


class LLMError(Exception):
    """大模型调用失败（网络 / 鉴权 / 上游业务错误 / 返回不可解析）。"""


def enabled() -> bool:
    return bool(settings.llm_enabled and settings.llm_base_url and settings.llm_api_key)


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        value = settings.llm_api_key
        if settings.llm_auth_scheme:
            value = f"{settings.llm_auth_scheme} {value}"
        headers[settings.llm_auth_header] = value
    return headers


def _content_of(obj: dict) -> str:
    """从 OpenAI 兼容结构里取正文：choices[0].message.content（或 delta）。"""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    content = (choice.get("message") or {}).get("content")
    if content is None:
        content = (choice.get("delta") or {}).get("content")
    if isinstance(content, list):
        return "".join(
            seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in content
        )
    return content or ""


def _raise_if_error(obj: dict) -> None:
    code = obj.get("code")
    if code not in (None, 0):
        raise LLMError(str(obj.get("message") or f"上游返回错误 code={code}"))


def _extract(raw: str) -> str:
    """解析响应体：优先按 SSE 的 data: 块取内容，否则整体当 JSON 解析。

    上游可能把 `event: error` 与 `data: {...}` 放在同一行，所以按 "data:" 切分、
    每段只取首行，而不是按行前缀匹配。
    """
    raw = raw or ""
    parts: list[str] = []
    for chunk in raw.split("data:")[1:]:
        lines = chunk.splitlines()
        data = lines[0].strip() if lines else ""
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        _raise_if_error(obj)
        parts.append(_content_of(obj))

    if parts:
        return "".join(parts).strip()

    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise LLMError("大模型返回内容无法解析")
    _raise_if_error(obj)
    return _content_of(obj).strip()


async def chat(
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.6,
    timeout: float | None = None,
    thinking: bool = True,
) -> str:
    """发起一次对话补全，返回纯文本正文。任何异常统一抛 LLMError。

    thinking=False 时请求关闭思维链（DeepSeek 推理模型会把大量 token 花在
    reasoning_content 上，关闭后同类任务耗时可减半）。
    """
    if not enabled():
        raise LLMError("大模型未启用或未配置 LLM_API_KEY")

    url = settings.llm_base_url.rstrip("/") + "/" + settings.llm_path.lstrip("/")
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "temperature": temperature,
        "stream": settings.llm_stream,
    }
    if not thinking:
        payload["thinking"] = {"type": "disabled"}

    try:
        async with httpx.AsyncClient(timeout=timeout or settings.llm_timeout) as client:
            resp = await client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise LLMError(f"大模型请求失败：{exc}") from exc

    body = resp.text or ""
    if resp.status_code >= 400:
        raise LLMError(f"大模型返回 HTTP {resp.status_code}：{body[:200]}")

    text = _extract(body)
    if not text:
        raise LLMError("大模型返回内容为空")
    return text


def loads_json(text: str):
    """从模型返回里取 JSON：容忍 ```json 代码块与前后说明文字。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start = min((i for i in (s.find("{"), s.find("[")) if i > -1), default=-1)
    if start > 0:
        end = max(s.rfind("}"), s.rfind("]"))
        if end > start:
            s = s[start : end + 1]
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        raise LLMError("大模型返回的不是合法 JSON")
