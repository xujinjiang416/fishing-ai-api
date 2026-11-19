"""weather.py — 钓鱼天气查询

缓存策略：同一地区 + 同一天只让大模型算一次，结果落 weather_cache 表；
后续（含其他用户）查同一地区同一天直接读库，避免重复消费。
"""

import datetime as dt
import json
import uuid
from copy import deepcopy

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_db, get_optional_user_id
from ...core.response import ok
from ...core.utils import now_text, today
from ...models import User, WeatherCache, WeatherLog
from ...services import ai, cache_policy

router = APIRouter(tags=["钓鱼天气"])


async def _find(db: AsyncSession, region: str, day: str) -> WeatherCache | None:
    return await db.scalar(
        select(WeatherCache).where(WeatherCache.region == region, WeatherCache.day == day)
    )


def _day_label(day: str) -> str:
    """今天 / 明天 / 后天；更远的日期直接回显 ISO 日期，供大模型理解语境。"""
    try:
        delta = (dt.date.fromisoformat(day) - dt.date.fromisoformat(today())).days
    except ValueError:
        return day
    return {0: "今天", 1: "明天", 2: "后天"}.get(delta, day)


async def _cached_weather(
    db: AsyncSession,
    region: str,
    day: str,
    place: str | None,
    detail: str | None,
    province: str | None,
    city: str | None,
    district: str | None,
) -> tuple[dict, bool]:
    """命中缓存直接返回；未命中（或缓存过期）则调实时天气源重新生成并落库。

    返回 (data, cache_hit)：cache_hit 表示本次是否命中缓存，用于命中率统计。
    """
    now = dt.datetime.now()
    row = await _find(db, region, day)

    # 缓存过期：删除旧记录走重新生成，避免旧数据源产物长期生效
    if row is not None and row.expire_at is not None and now >= row.expire_at:
        await db.delete(row)
        await db.commit()
        row = None

    if row is None:
        # 真实天气源/大模型生成天气+鱼情，返回实际数据来源
        data, source = await ai.generate_weather_traced(place, detail, day)
        data = await ai.judge_weather(data, _day_label(day))
        ttl = await cache_policy.ttl_for(db, province=province, city=city, district=district)
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        db.add(
            WeatherCache(
                region=region,
                day=day,
                payload=data,
                source=source,
                expire_at=now + dt.timedelta(seconds=ttl),
                size_bytes=size,
                hit_count=0,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # 并发下别的请求已写入，回滚后直接用它的结果
            await db.rollback()
            row = await _find(db, region, day)
            data = deepcopy(row.payload) if row else data
        cache_hit = False
    else:
        data = deepcopy(row.payload)
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = now
        await db.commit()
        cache_hit = True

    # 地点与更新时间属于「本次请求」的信息，按当前入参重算，缓存只固化天气数值与研判
    fresh = ai.build_weather(place, detail, day)["weather"]
    data["weather"].update(
        place=fresh["place"], placeDetail=fresh["placeDetail"], updated=fresh["updated"]
    )
    return data, cache_hit


@router.get("/weather", summary="查天气 + 鱼情研判 + 全天时段概率")
async def weather(
    lat: float | None = None,
    lng: float | None = None,
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    date: str | None = None,
    user_id: int | None = Depends(get_optional_user_id),
    db: AsyncSession = Depends(get_db),
):
    """参数二选一：经纬度，或省/市/区。未登录也可查询，登录后带上个性化偏好。"""
    if not (province or city or district) and user_id:
        user = await db.get(User, user_id)
        loc = (user.location or {}) if user else {}
        province, city, district = loc.get("province"), loc.get("city"), loc.get("district")

    place = " ".join([x for x in (province, city, district) if x])
    detail = f"{district or city or '当前位置'} · 3 公里范围内" if place else None
    # 缓存维度精确到「区」：同一个城市的不同区，天气与鱼情研判各不相同
    region = "".join(x for x in (city, district) if x) or (province or "默认")
    day = (date or today())[:10]

    data, cache_hit = await _cached_weather(db, region, day, place, detail, province, city, district)
    data["query"] = {"lat": lat, "lng": lng, "date": date}

    # 落一条天气查询日志，供管理后台查看 / 重新生成（失败不影响主流程）
    try:
        w = data["weather"]
        v = data["verdict"]
        user = await db.get(User, user_id) if user_id else None
        # 身份成本：会员 / 普通用户（已移除广告解锁、单次付费解锁，统一记为「普通用户」）
        cost = "会员" if (user and user.is_member) else "普通用户"
        db.add(
            WeatherLog(
                id=f"wx_{uuid.uuid4().hex[:12]}",
                user_id=user.id if user else None,
                user=user.nickname if user else "游客",
                place=w.get("place"),
                place_detail=w.get("placeDetail"),
                date=now_text(),
                score=int(v.get("score") or 0),
                verdict=v.get("name"),
                level=v.get("level"),
                condition=w.get("condition"),
                temp=w.get("temp"),
                pressure=w.get("pressure"),
                wind_diff=f"{w.get('windDir', '')}{w.get('windLevel', '')}",
                source="手机定位" if (lat or lng) else "手动选点",
                cost=cost,
                cache_hit=cache_hit,
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
    return ok(data)
