"""cache_policy.py — 天气 / 鱼情本地缓存策略（统一 3 小时有效期）

规则（对全流程生效，无例外）：
- 天气数据（和风天气等上游）与鱼情分析（大模型）各自独立缓存，有效期均为 3 小时；
- 天气缓存键：标准化地址（省/市/区去空白、去重）+ 查询日期；
- 鱼情缓存键：全部查询参数规范化 JSON 的 SHA-256；
- 过期条目在下一次同名查询时自动失效并重新拉取，同步重置有效期，无需人工干预；
- 默认规则固定 3 小时，可在管理后台「缓存规则」查看 / 调整（持久化在 app_configs）。

职责：
- 定义缓存键生成规则（天气按地址+日期，鱼情按参数哈希）；
- 提供 TTL 规则的读取 / 保存 / 默认值；
- 提供统一的过期判定函数，供 cache_store 复用。

关键数据流：
  normalize_address → weather_cache_key / fish_cache_key → cache_store 查表
  get_rules（读 app_configs）→ weather_ttl / fish_ttl → cache_store 计算 expire_at

依赖：
- 数据库 app_configs 表（SQLAlchemy AsyncSession）：持久化 TTL 规则；
- 无外部网络依赖，纯本地计算。

失败降级策略：
- 配置缺失 / JSON 非法 / 值非法时一律回退默认 3 小时，不阻断查询；
- 单条规则非法只跳过该条，其余规则继续生效。

注意事项：
- 本模块只负责「策略」，不直接读写缓存表（读写见 cache_store）；
- 所有时间比较使用本地时间 `datetime.now()`，expire_at 也按本地时间写入。
"""

import datetime as dt
import hashlib
import json
import re
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppConfig

# app_configs 表中存放 TTL 规则的键名与备注（后台展示用）
RULES_KEY = "cache_ttl_rules"
RULES_REMARK = "天气 / 鱼情缓存有效期（秒，默认 3 小时）"

# 统一缓存有效期：3 小时（秒），作为所有规则的基准默认值
CACHE_TTL_SECONDS = 3 * 60 * 60

# 默认规则（秒）：weather=天气数据，fish=鱼情分析，两者默认均为 3 小时
DEFAULT_RULES: dict[str, int] = {"weather": CACHE_TTL_SECONDS, "fish": CACHE_TTL_SECONDS}

# 规则项中文名（供后台展示）
RULE_LABELS = {"weather": "天气数据缓存", "fish": "鱼情分析缓存"}


# ---------------- 地址标准化与缓存键 ----------------

def _clean_part(value) -> str:
    """去掉全部空白字符（含全角空格），统一为紧凑文本。

    Args:
        value: 原始地址片段，可为 None。

    Returns:
        str: 去除所有空白后的字符串。
    """
    return re.sub(r"\s+", "", str(value or "").strip())


def normalize_address(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    place: str | None = None,
) -> str:
    """标准化地址：省/市/区依次去空白与重复项（直辖市省级=市级时只保留一次）。

    无结构化地址时回退按空格切分 place；仍为空则统一记为「默认」。

    Args:
        province: 省级名称，可为空。
        city: 市级名称，可为空。
        district: 区县名称，可为空。
        place: 非结构化地点字符串，仅在省市区全空时使用。

    Returns:
        str: 以单空格连接的标准化地址；全空时返回「默认」。
    """
    parts: list[str] = []
    for raw in (province, city, district):
        p = _clean_part(raw)
        if p and (not parts or parts[-1] != p):
            parts.append(p)
    if not parts and place:
        for raw in str(place).split():
            p = _clean_part(raw)
            if p and (not parts or parts[-1] != p):
                parts.append(p)
    return " ".join(parts) or "默认"


def weather_cache_key(
    *,
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    place: str | None = None,
    day: str | None = None,
) -> str:
    """天气缓存键：标准化地址 + 查询日期（同一地址同一天精准匹配同一条目）。

    Args:
        province/city/district/place: 地址片段，参与标准化。
        day: 查询日期（ISO），仅取前 10 位。

    Returns:
        str: ``地址|YYYY-MM-DD`` 形式的缓存键。
    """
    return f"{normalize_address(province, city, district, place=place)}|{str(day or '')[:10]}"


def canonical_json(value) -> str:
    """稳定序列化：键排序 + 紧凑分隔符，保证相同参数得到相同字符串。

    Args:
        value: 任意可 JSON 序列化的结构。

    Returns:
        str: 键有序、无多余空格的 JSON 文本。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fish_cache_key(params: dict) -> str:
    """鱼情缓存键：全部查询参数序列化后的 SHA-256，避免不同条件误命中。

    Args:
        params: 查询参数字典（赛道、钓法、地址等）。

    Returns:
        str: ``fish:`` 前缀 + 64 位 SHA-256 十六进制摘要。
    """
    digest = hashlib.sha256(canonical_json(params or {}).encode("utf-8")).hexdigest()
    return f"fish:{digest}"


def is_expired(expire_at: dt.datetime | None, now: dt.datetime | None = None) -> bool:
    """有效期判定：未设置过期时间视为有效；否则按当前时间比较。

    Args:
        expire_at: 条目过期时间，None 表示永不过期。
        now: 比较基准时间，缺省取本地当前时间。

    Returns:
        bool: True 表示已过期。
    """
    if expire_at is None:
        return False
    return (now or dt.datetime.now()) >= expire_at


# ---------------- 缓存过期规则（管理后台可配置） ----------------

def default_rules() -> dict:
    """返回默认 TTL 规则的深拷贝（调用方可安全修改）。"""
    return deepcopy(DEFAULT_RULES)


async def get_rules(db: AsyncSession) -> dict:
    """读取 TTL 规则；缺失 / 非法 / 旧版本结构时回退默认 3 小时。

    每条规则最小 60 秒，避免后台误填 0 导致缓存永久失效。

    Args:
        db: 异步数据库会话。

    Returns:
        dict: ``{"weather": 秒, "fish": 秒}``。
    """
    rules = default_rules()
    row = await db.scalar(select(AppConfig).where(AppConfig.key == RULES_KEY))
    if not row or not row.value:
        return rules
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return rules
    if isinstance(data, dict):
        for key in rules:
            if key in data:
                try:
                    rules[key] = max(60, int(data[key]))
                except (TypeError, ValueError):
                    continue
    return rules


async def save_rules(db: AsyncSession, rules: dict) -> None:
    """写入 TTL 规则（调用方负责 commit）。

    Args:
        db: 异步数据库会话。
        rules: 待写入的规则 dict；仅保留 DEFAULT_RULES 中登记的键。

    Returns:
        None
    """
    payload = json.dumps(
        {k: int(rules[k]) for k in DEFAULT_RULES if k in rules}, ensure_ascii=False
    )
    row = await db.scalar(select(AppConfig).where(AppConfig.key == RULES_KEY))
    if row is None:
        db.add(AppConfig(key=RULES_KEY, value=payload, remark=RULES_REMARK))
    else:
        row.value = payload
        row.remark = RULES_REMARK


async def weather_ttl(db: AsyncSession) -> int:
    """天气数据缓存有效期（秒）。

    Args:
        db: 异步数据库会话。

    Returns:
        int: 天气缓存 TTL（秒）。
    """
    return (await get_rules(db))["weather"]


async def fish_ttl(db: AsyncSession) -> int:
    """鱼情分析缓存有效期（秒）。

    Args:
        db: 异步数据库会话。

    Returns:
        int: 鱼情缓存 TTL（秒）。
    """
    return (await get_rules(db))["fish"]
