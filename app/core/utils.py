"""utils.py — 时间 / ID 等通用小工具"""

import time
from datetime import date, datetime, timedelta
from uuid import uuid4


def today() -> str:
    return date.today().isoformat()


def today_cn() -> str:
    """前端展示用的日期：2026-11-05 周三"""
    d = date.today()
    return f"{d.isoformat()} 周{'一二三四五六日'[d.weekday()]}"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def gen_no(prefix: str = "F") -> str:
    """订单号：F + 年月日 + 时分秒毫秒"""
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S%f")[:16]


def gen_id(prefix: str = "c") -> str:
    """业务短 ID（评论等），保证同一毫秒内也不重复"""
    return f"{prefix}{int(time.time() * 1000):x}{uuid4().hex[:6]}"


def add_days(date_str: str | None, days: int) -> str:
    base = datetime.fromisoformat(date_str).date() if date_str else date.today()
    if base < date.today():
        base = date.today()
    return (base + timedelta(days=days)).isoformat()
