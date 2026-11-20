"""cache_store.py — 天气数据 / 鱼情分析本地缓存读写

缓存职责：
- 天气数据：按「标准化地址 + 日期」缓存上游天气源结果，3 小时内重复查询直接命中；
- 鱼情分析：按「全部查询参数哈希」缓存大模型结论，3 小时内重复请求直接命中；
- 过期条目在下次同名查询时自动失效并重新拉取，同步重置有效期（无需人工干预）；
- 定期清理全库过期条目，避免无效缓存长期占用空间。

关键数据流：
- get_weather：cache_policy.weather_cache_key → 查 WeatherCache；
  命中且未过期直接返回 payload.weather，否则调用 ai.generate_weather_traced
  生成后 upsert 再返回。
- get_fish：cache_policy.fish_cache_key → 查 FishCache；
  命中且未过期直接返回 payload，否则调用 ai.judge_weather 生成后 upsert 再返回。

依赖：
- 数据库 weather_caches / fish_caches 表（SQLAlchemy AsyncSession）；
- app.services.ai（上游天气 / 大模型）；
- app.services.cache_policy（缓存键与 TTL）。

失败降级策略：
- 上游异常由 ai 内部逐级降级，仍返回完整结构；
- 并发写入触发 IntegrityError 时回滚并尽量复用已有行，缓存不中断；
- 清理任务异常不影响主查询流程。

注意事项：
- 命中时只更新 hit_count/last_hit_at，不刷新 expire_at，保证 3 小时窗口固定；
- 过期重建会把 hit_count 归零并重置 created_at；
- 会话 commit 由本模块负责，与业务写入分离。
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

# 全库过期条目清理的最小间隔（秒）：默认 10 分钟，避免每次查询都做全表删除。
PURGE_INTERVAL_SECONDS = 10 * 60
# 上次清理时间戳（进程内变量，单实例有效）；0 表示尚未清理。
_last_purge_at: float = 0.0

# 天气缓存结构版本：天气字段口径或推导规则变化时递增，旧结构条目自动失效重建。
# v3：天气字段口径调整，降水量改为今日累计值。
WEATHER_CACHE_VERSION = 3

# 鱼情缓存结构版本：评分口径变化时递增，旧结论（含旧文案）自动失效重建。
# v4：时段口型改为按时段概率 + 统一等级阈值重算。
FISH_CACHE_VERSION = 4

# 参与鱼情研判的天气字段：任一变化都应产生新的鱼情缓存键，避免天气更新后仍返回旧结论。
_FISH_WEATHER_KEYS = (
    "temp", "tempMax", "tempMin", "tempFeel",
    "pressure", "pressure6h", "pressureTrend",
    "humidity", "windSpeed", "uv",
)


def _cached_verdict_is_valid(payload: dict) -> bool:
    """缓存中的鱼情结论是否仍符合当前等级表与权重口径。

    旧缓存可能来自调整前的阈值 / 等级名称，或总分与分项加权和不一致；
    这类条目继续返回会让前端长期显示过期结论，因此视为未命中并重建。

    Args:
        payload: 缓存 payload（含 verdict / slots / aiGenerated）。

    Returns:
        bool: True 表示可继续使用。
    """
    verdict = (payload or {}).get("verdict") or {}
    try:
        score = int(verdict.get("score"))
    except (TypeError, ValueError):
        return False

    level = ai.verdict_of(score)
    if verdict.get("level") != level["level"] or verdict.get("name") != level["name"]:
        return False

    dims = verdict.get("dimensions") or []
    if len(dims) == 4:
        weighted = round(
            sum(float(d.get("score") or 0) * float(d.get("weight") or 0) for d in dims)
        )
        if weighted != score:
            return False
    return True


def _size_of(payload) -> int:
    """估算缓存占用字节数（JSON 序列化 UTF-8）。

    Args:
        payload: 待估算的缓存结构。

    Returns:
        int: UTF-8 字节数；不可序列化时返回 0。
    """
    try:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


async def purge_expired(db: AsyncSession, *, force: bool = False) -> int:
    """清理全部过期缓存（天气 + 鱼情）；默认 10 分钟内最多执行一次。

    Args:
        db: 异步数据库会话。
        force: 为 True 时忽略间隔限制立即清理（后台手动触发用）。

    Returns:
        int: 本次删除的天气 + 鱼情条目总数；被间隔限制跳过时返回 0。
    """
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
    """读取天气缓存；未命中 / 已过期则调用上游生成并写入（返回 (weather, cache_hit)）。

    命中策略：
    - 缓存键 = 标准化地址 + 日期，因此同地址同日期必命中同一条目；
    - 命中仅累加 hit_count / 刷新 last_hit_at，不延长 expire_at；
    - 未命中或过期则重新拉取上游，并按最新 TTL 重置 expire_at。

    Args:
        db: 异步数据库会话。
        province/city/district/place/detail: 地址信息（place 参与缓存键）。
        day: 查询日期（ISO）。

    Returns:
        tuple[dict, bool]: (camelCase 的 weather 数据, 是否命中缓存)。
    """
    key = cache_policy.weather_cache_key(
        province=province, city=city, district=district, place=place, day=day
    )
    region = cache_policy.normalize_address(province, city, district, place=place)
    now = dt.datetime.now()

    row = await db.scalar(select(WeatherCache).where(WeatherCache.cache_key == key))

    if (
        row is not None
        and not cache_policy.is_expired(row.expire_at, now)
        and (row.payload or {}).get("version") == WEATHER_CACHE_VERSION
    ):
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = now
        await db.commit()
        return deepcopy((row.payload or {}).get("weather") or {}), True

    # 未命中 / 已过期 / 旧结构：重新调用上游天气源
    data, source = await ai.generate_weather_traced(place, detail, day)
    payload = {"version": WEATHER_CACHE_VERSION, "weather": data.get("weather") or {}}
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
        if (
            row is not None
            and not cache_policy.is_expired(row.expire_at, dt.datetime.now())
            and (row.payload or {}).get("version") == WEATHER_CACHE_VERSION
        ):
            return deepcopy((row.payload or {}).get("weather") or {}), True
    return deepcopy(payload["weather"]), False


async def rebuild_weather(db: AsyncSession, row: WeatherCache) -> str:
    """强制重建一条天气缓存的 payload（供后台手动刷新），返回数据来源。

    Args:
        db: 异步数据库会话。
        row: 已存在的 WeatherCache 行（会被原地更新，调用方负责 commit）。

    Returns:
        str: 数据来源标记（qweather / llm / fallback）。
    """
    place = str(
        ((row.payload or {}).get("weather") or {}).get("place") or row.region or ""
    )
    detail = f"{row.region} · 3 公里范围内" if row.region else None
    data, source = await ai.generate_weather_traced(place, detail, row.day)
    payload = {"version": WEATHER_CACHE_VERSION, "weather": data.get("weather") or {}}
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

    命中策略：
    - 缓存键 = 查询参数的 SHA-256；payload 内记录参与研判的天气指纹，
      天气字段发生变化时视为未命中并原地重建，避免返回旧天气下的结论；
    - 命中仅累加 hit_count / 刷新 last_hit_at，不延长 expire_at；
    - 未命中或过期则调用 ai.judge_weather（内部自动回退规则引擎）。

    Args:
        db: 异步数据库会话。
        params: 查询参数（用于生成缓存键与 region）。
        weather: camelCase 天气数据，作为研判输入。
        day_label: 中文日期标签。

    Returns:
        tuple[dict, bool]: ({verdict, slots, aiGenerated}, 是否命中缓存)。
    """
    fingerprint = cache_policy.canonical_json(
        {k: weather.get(k) for k in _FISH_WEATHER_KEYS}
    )
    key = cache_policy.fish_cache_key(params)
    region = cache_policy.normalize_address(
        params.get("province"), params.get("city"), params.get("district"),
        place=params.get("place"),
    )
    now = dt.datetime.now()

    row = await db.scalar(select(FishCache).where(FishCache.cache_key == key))
    if (
        row is not None
        and not cache_policy.is_expired(row.expire_at, now)
        and (row.payload or {}).get("version") == FISH_CACHE_VERSION
        and (row.payload or {}).get("weatherFingerprint") == fingerprint
        and _cached_verdict_is_valid(row.payload)
    ):
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = now
        await db.commit()
        return deepcopy(row.payload or {}), True

    # 未命中 / 过期 / 天气变化 / 评分口径升级：调用大模型（失败时内部自动回退规则引擎）
    data = await ai.judge_weather({"weather": deepcopy(weather)}, day_label)
    payload = {
        "version": FISH_CACHE_VERSION,
        "verdict": data.get("verdict") or {},
        "slots": data.get("slots") or [],
        "aiGenerated": bool(data.get("aiGenerated")),
        "weatherFingerprint": fingerprint,
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
        if (
            row is not None
            and not cache_policy.is_expired(row.expire_at, dt.datetime.now())
            and (row.payload or {}).get("version") == FISH_CACHE_VERSION
            and (row.payload or {}).get("weatherFingerprint") == fingerprint
            and _cached_verdict_is_valid(row.payload)
        ):
            return deepcopy(row.payload or {}), True
    return deepcopy(payload), False
