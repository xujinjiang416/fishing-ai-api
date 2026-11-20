"""response.py — 统一响应体

职责：为所有接口提供统一的成功 / 失败响应结构，避免各路由自行拼装返回体。

契约：{ code, data, msg }，code = 0 表示成功，非 0 即业务失败或 HTTP 错误码。
前端 js/api.js / common/api.js 已按此结构消费，禁止改动字段名。

注意事项：
    本模块只负责组装 dict，不负责序列化；由 FastAPI 统一转成 JSON。
"""

from typing import Any


def ok(data: Any = None, msg: str = "ok") -> dict:
    """构造成功响应。

    Args:
        data: 业务数据，可为任意可 JSON 序列化的结构；无数据时传 None。
        msg: 提示文案，默认 "ok"。

    Returns:
        dict: { code: 0, data, msg }，code 恒为 0 表示成功。
    """
    return {"code": 0, "data": data, "msg": msg}


def fail(code: int, msg: str, data: Any = None) -> dict:
    """构造失败响应。

    Args:
        code: 业务错误码或 HTTP 状态码，非 0。
        msg: 面向用户的错误文案。
        data: 可选附加数据（如错误明细），默认 None。

    Returns:
        dict: { code, data, msg }。
    """
    return {"code": code, "data": data, "msg": msg}
