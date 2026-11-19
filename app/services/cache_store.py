"""cache_store.py — 天气数据 / 鱼情分析本地缓存读写

缓存职责：
- 天气数据：按「标准化地址 + 日期」缓存上游天气源结果，3 小时内重复查询直接命中；
- 鱼情分析：按「全部查询参数哈希」缓存大模型结论，3 小时内重复请求直接命中；
- 过期条目在下次同名查询时自动失效并重新拉取，同步重置有效期（无需人工干预）；
- 定期清理全库过期条目，避免无效缓存长期占用空间。
"""

import datetime as dt
import json
import time
from copy import deepcopy

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FishCache, WeatherCache
from . import ai, cache_policy

# 全库过期条目清理的最小间隔（秒）
PURGE_INTERVAL_SECONDS = 10 * 60
_last_purge_at: float = 0.0


def _size_of(payload) -> int:
    """估算缓存占用字节数（JSON 序列化 UTF-8）。"""
    try:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


async def purge_expired(db: AsyncSession, *, force: bool = False) -> int:
    """清理全部过期缓存（天气 + 鱼情）；默认 10 分钟内最多执行一次。"""
    global _last_purge_at
    now_ts = time.time()
    if not force and now_ts - _last_purge_at < PURGE_INTERVAL_SECONDS:
        return 0
    _last_purge_at = now_ts

    now = dt.datetime.now()
    w = await db.execute(
        delete(WeatherCache).where(
            WeatherCache.expire_at.is_not(None), WeatherCache.expire_at <= now
        )
    )
    f = await db.execute(
        delete(FishCache).where(FishCache.expire_at.is_not(None), FishCache.expire_at <= now)
    )
    await db.commit()
    return int(w.rowcount or 0) + int(f.rowcount or 0)


# ---------------- 天气数据缓存 ----------------

async def get_weather(
    db: AsyncSession,
    *,
    province: str | None,
    city: str | None,
    district: str | None,
    place: str | None,
    detail: str | None,
    day: str,
) -> tuple[dict, bool]:
    """读取天气缓存；未命中 / 已过期则调用上游生成并写入（返回 (weather, cache_hit)）。"""
    key = cache_policy.weather_cache_key(
        province=province, city=city, district=district, place=place, day=day
    )
    region = cache_policy.normalize_address(province, city, district, place=place)
    now = dt.datetime.now()

    row = await db.scalar(select(WeatherCache).where(WeatherCache.cache_key == key))

    if row is not None and not cache_policy.is_expired(row.expire_at, now):
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = now
        await db.commit()
        return deepcopy((row.payload or {}).get("weather") or {}), True

    # 未命中或已过期：重新调用上游天气源
    data, source = await ai.generate_weather_traced(place, detail, day)
    payload = {"weather": data.get("weather") or {}}
    ttl = await cache_policy.weather_ttl(db)
    expire_at = now + dt.timedelta(seconds=ttl)

    if row is None:
        db.add(
            WeatherCache(
                cache_key=key,
                region=region,
                day=day,
                payload=payload,
                source=source,
                expire_at=expire_at,
                size_bytes=_size_of(payload),
                hit_count=0,
            )
        )
    else:
        row.cache_key = key
        row.region = region
        row.payload = payload
        row.source = source
        row.expire_at = expire_at
        row.size_bytes = _size_of(payload)
        row.hit_count = 0
        row.last_hit_at = None
        row.created_at = now

    try:
        await db.commit()
    except IntegrityError:
        # 并发写入 / 历史数据冲突：回滚后尽量复用已有行，保证缓存仍然生效
        await db.rollback()
        row = await db.scalar(select(WeatherCache).where(WeatherCache.cache_key == key))
        if row is None:
            row = await db.scalar(
                select(WeatherCache).where(
                    WeatherCache.region == region, WeatherCache.day == day
                )
            )
            if row is not None:
                row.cache_key = key
                row.payload = payload
                row.source = source
                row.expire_at = expire_at
                row.size_bytes = _size_of(payload)
                row.hit_count = 0
                row.last_hit_at = None
                row.created_at = now
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.rollback()
        if row is not None and not cache_policy.is_expired(row.expire_at, dt.datetime.now()):
            return deepcopy((row.payload or {}).get("weather") or {}), True
    return deepcopy(payload["weather"]), False


async def rebuild_weather(db: AsyncSession, row: WeatherCache) -> str:
    """强制重建一条天气缓存的 payload（供后台手动刷新），返回数据来源。"""
    place = str(
        ((row.payload or {}).get("weather") or {}).get("place") or row.region or ""
    )
    detail = f"{row.region} · 3 公里范围内" if row.region else None
    data, source = await ai.generate_weather_traced(place, detail, row.day)
    payload = {"weather": data.get("weather") or {}}
    ttl = await cache_policy.weather_ttl(db)
    now = dt.datetime.now()

    # 兼容历史数据：补齐缓存键，保证后续同一地址查询能命中该条目
    row.cache_key = cache_policy.weather_cache_key(place=place, day=row.day)
    row.payload = payload
    row.source = source
    row.expire_at = now + dt.timedelta(seconds=ttl)
    row.size_bytes = _size_of(payload)
    row.hit_count = 0
    row.last_hit_at = None
    row.created_at = now
    return source


# ---------------- 鱼情分析缓存 ----------------

async def get_fish(
    db: AsyncSession,
    *,
    params: dict,
    weather: dict,
    day_label: str,
) -> tuple[dict, bool]:
    """读取鱼情分析缓存；未命中 / 已过期则调用大模型并写入。

    返回 ({verdict, slots, aiGenerated}, cache_hit)。
    """
    key = cache_policy.fish_cache_key(params)
    region = cache_policy.normalize_address(
        params.get("province"), params.get("city"), params.get("district"),
        place=params.get("place"),
    )
    now = dt.datetime.now()

    row = await db.scalar(select(FishCache).where(FishCache.cache_key == key))
    if row is not None and not cache_policy.is_expired(row.expire_at, now):
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = now
        await db.commit()
        return deepcopy(row.payload or {}), True

    # 未命中或已过期：调用大模型（失败时内部自动回退规则引擎）
    data = await ai.judge_weather({"weather": deepcopy(weather)}, day_label)
    payload = {
        "verdict": data.get("verdict") or {},
        "slots": data.get("slots") or [],
        "aiGenerated": bool(data.get("aiGenerated")),
    }
    source = "llm" if payload["aiGenerated"] else "rule"
    ttl = await cache_policy.fish_ttl(db)
    expire_at = now + dt.timedelta(seconds=ttl)

    if row is None:
        db.add(
            FishCache(
                cache_key=key,
                region=region,
                params=params,
                payload=payload,
                source=source,
                expire_at=expire_at,
                size_bytes=_size_of(payload),
                hit_count=0,
            )
        )
    else:
        row.region = region
        row.params = params
        row.payload = payload
        row.source = source
        row.expire_at = expire_at
        row.size_bytes = _size_of(payload)
        row.hit_count = 0
        row.last_hit_at = None
        row.created_at = now

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        row = await db.scalar(select(FishCache).where(FishCache.cache_key == key))
        if row is not None and not cache_policy.is_expired(row.expire_at, dt.datetime.now()):
            return deepcopy(row.payload or {}), True
    return deepcopy(payload), False
