"""response.py — 统一响应体

契约：{ code, data, msg }，code = 0 表示成功。
前端 js/api.js / common/api.js 已按此结构消费，禁止改动字段名。
"""

from typing import Any


def ok(data: Any = None, msg: str = "ok") -> dict:
    return {"code": 0, "data": data, "msg": msg}


def fail(code: int, msg: str, data: Any = None) -> dict:
    return {"code": code, "data": data, "msg": msg}
