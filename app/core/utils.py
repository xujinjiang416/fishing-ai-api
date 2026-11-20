"""utils.py — 时间 / ID 等通用小工具

职责：提供业务中反复使用的时间格式化、日期计算与业务编号生成函数。

约定：
    - 日期统一使用 `YYYY-MM-DD` 字符串（与数据库 String(10) 列一致）；
    - 时间展示统一使用 `YYYY-MM-DD HH:MM`；
    - 本模块全部为纯函数，不访问数据库、不读取配置。

注意事项：
    - 服务端以本机时区为准（部署时建议设置为 Asia/Shanghai）；
    - 生成的业务编号只保证「单机内基本唯一」，不作为强唯一性依据（库表另有唯一索引）。
"""

import time
from datetime import date, datetime, timedelta
from uuid import uuid4


def today() -> str:
    """获取今天的日期字符串，用于签到/广告额度等按天判断的场景。

    Returns:
        str: `YYYY-MM-DD` 格式的当日日期。
    """
    return date.today().isoformat()


def today_cn() -> str:
    """前端展示用的日期：2026-11-05 周三

    Returns:
        str: `YYYY-MM-DD 周X`，星期取自 date.weekday()（0 = 周一）。
    """
    d = date.today()
    return f"{d.isoformat()} 周{'一二三四五六日'[d.weekday()]}"


def now_text() -> str:
    """当前时间文案（分钟精度），用于日志 / 展示类字段。

    Returns:
        str: `YYYY-MM-DD HH:MM` 格式的当前时间。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def gen_no(prefix: str = "F") -> str:
    """订单号：F + 年月日 + 时分秒毫秒

    Args:
        prefix: 业务前缀，默认 "F"（订单），会员订单等可传其他前缀。

    Returns:
        str: 长度 17 的编号；时间部分取 `%Y%m%d%H%M%S%f` 的前 16 位
        （即年月日时分秒 + 微秒的前两位，百分之一秒精度）。
    """
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S%f")[:16]


def gen_id(prefix: str = "c") -> str:
    """业务短 ID（评论等），保证同一毫秒内也不重复

    组成：前缀 + 毫秒时间戳（16 进制）+ uuid4 前 6 位，兼顾可读性与唯一性。

    Args:
        prefix: 资源类型前缀，默认 "c"（评论）。

    Returns:
        str: 业务主键字符串。
    """
    return f"{prefix}{int(time.time() * 1000):x}{uuid4().hex[:6]}"


def add_days(date_str: str | None, days: int) -> str:
    """在某日期基础上加若干天，用于会员到期时间计算。

    基准日规则：传入日期早于今天（或未传）时，按今天起算，避免续费得到过去的到期日。

    Args:
        date_str: 基准日期 `YYYY-MM-DD`；为 None 表示从今天开始。
        days: 需要增加的天数（可为负，表示往前推算）。

    Returns:
        str: 计算后的日期 `YYYY-MM-DD`。
    """
    # 解析基准日：未传日期时取今天
    base = datetime.fromisoformat(date_str).date() if date_str else date.today()
    # 已过期则从今天续算，保证结果不会早于今天
    if base < date.today():
        base = date.today()
    return (base + timedelta(days=days)).isoformat()
