"""cache_policy.py — 天气 / 鱼情本地缓存策略（统一 3 小时有效期）

规则（对全流程生效，无例外）：
- 天气数据（和风天气等上游）与鱼情分析（大模型）各自独立缓存，有效期均为 3 小时；
- 天气缓存键：标准化地址（省/市/区去空白、去重）+ 查询日期；
- 鱼情缓存键：全部查询参数规范化 JSON 的 SHA-256；
- 过期条目在下一次同名查询时自动失效并重新拉取，同步重置有效期，无需人工干预；
- 默认规则固定 3 小时，可在管理后台「缓存规则」查看 / 调整（持久化在 app_configs）。
"""

import datetime as dt
import hashlib
import json
import re
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppConfig

RULES_KEY = "cache_ttl_rules"
RULES_REMARK = "天气 / 鱼情缓存有效期（秒，默认 3 小时）"

# 统一缓存有效期：3 小时
CACHE_TTL_SECONDS = 3 * 60 * 60

# 默认规则（秒）
DEFAULT_RULES: dict[str, int] = {"weather": CACHE_TTL_SECONDS, "fish": CACHE_TTL_SECONDS}

# 规则项中文名（供后台展示）
RULE_LABELS = {"weather": "天气数据缓存", "fish": "鱼情分析缓存"}


# ---------------- 地址标准化与缓存键 ----------------

def _clean_part(value) -> str:
    """去掉全部空白字符（含全角空格），统一为紧凑文本。"""
    return re.sub(r"\s+", "", str(value or "").strip())


def normalize_address(
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    place: str | None = None,
) -> str:
    """标准化地址：省/市/区依次去空白与重复项（直辖市省级=市级时只保留一次）。

    无结构化地址时回退按空格切分 place；仍为空则统一记为「默认」。
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
    """天气缓存键：标准化地址 + 查询日期（同一地址同一天精准匹配同一条目）。"""
    return f"{normalize_address(province, city, district, place=place)}|{str(day or '')[:10]}"


def canonical_json(value) -> str:
    """稳定序列化：键排序 + 紧凑分隔符，保证相同参数得到相同字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fish_cache_key(params: dict) -> str:
    """鱼情缓存键：全部查询参数序列化后的 SHA-256，避免不同条件误命中。"""
    digest = hashlib.sha256(canonical_json(params or {}).encode("utf-8")).hexdigest()
    return f"fish:{digest}"


def is_expired(expire_at: dt.datetime | None, now: dt.datetime | None = None) -> bool:
    """有效期判定：未设置过期时间视为有效；否则按当前时间比较。"""
    if expire_at is None:
        return False
    return (now or dt.datetime.now()) >= expire_at


# ---------------- 缓存过期规则（管理后台可配置） ----------------

def default_rules() -> dict:
    return deepcopy(DEFAULT_RULES)


async def get_rules(db: AsyncSession) -> dict:
    """读取 TTL 规则；缺失 / 非法 / 旧版本结构时回退默认 3 小时。"""
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
    """写入 TTL 规则（调用方负责 commit）。"""
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
    """天气数据缓存有效期（秒）。"""
    return (await get_rules(db))["weather"]


async def fish_ttl(db: AsyncSession) -> int:
    """鱼情分析缓存有效期（秒）。"""
    return (await get_rules(db))["fish"]
