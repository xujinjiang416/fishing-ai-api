"""weather.py — 钓鱼天气查询

职责：小程序天气页唯一数据入口，一次请求同时返回天气数据、鱼情研判结论与
全天时段出钓概率。

鉴权要求：公开接口，登录可选；登录且未传行政区划时按用户定位查询。

依赖服务：
- 上游天气源 QWeather（经 services.ai 封装；未配置 key / host 时退回大模型或本地规则兜底）；
- 大模型（services.ai.judge_weather，失败时内部自动回退规则引擎）；
- MySQL（weather_cache / fish_cache / weather_logs 表，由 services.cache_store 读写）。

缓存策略（统一 3 小时有效期，全流程生效）：
- 天气数据：按「标准化地址 + 日期」缓存，3 小时内重复查询不再调用和风天气；
- 鱼情分析：按「全部查询参数哈希」缓存，3 小时内重复请求不再调用大模型；
- 过期后下一次查询自动重新拉取并重置有效期，无需人工干预。

注意事项：缓存清理、日志落库等旁路操作失败均不影响主流程返回。
"""

import datetime as dt
import uuid
from copy import deepcopy

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_db, get_optional_user_id
from ...core.response import ok
from ...core.utils import now_text, today
from ...models import User, WeatherLog
from ...services import ai, cache_store

router = APIRouter(tags=["钓鱼天气"])


def _day_label(day: str) -> str:
    """今天 / 明天 / 后天；更远的日期直接回显 ISO 日期，供大模型理解语境。"""
    try:
        delta = (dt.date.fromisoformat(day) - dt.date.fromisoformat(today())).days
    except ValueError:
        return day
    return {0: "今天", 1: "明天", 2: "后天"}.get(delta, day)


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
    """查询天气 + 鱼情研判 + 全天时段概率。

    参数二选一：经纬度，或省/市/区。未登录也可查询，登录后带上个性化偏好。
    方法与路径：GET /weather（可选登录）。
    参数：lat / lng 经纬度；province / city / district 行政区划（优先结构化地址）；
    date 查询日期 ISO 字符串（缺省今天，仅取前 10 位）；未传地址且已登录时回退用户定位。
    响应：{ weather, verdict, slots, aiGenerated, query }；query 回显经纬度与原始日期。
    处理顺序与降级：
    1) 先清理过期缓存（cache_store 内部 10 分钟节流，避免每次全表扫描）；
    2) 天气按「标准化地址 + 日期」缓存，命中则跳过上游；地点与更新时间按本次入参重算；
    3) 鱼情按全部查询参数哈希缓存，命中则跳过 LLM；未命中且 LLM 失败时由 ai 层回退规则引擎；
    4) 写一条 WeatherLog（记录是否全命中），写库失败仅回滚日志、不影响响应。
    """
    if not (province or city or district) and user_id:
        user = await db.get(User, user_id)
        loc = (user.location or {}) if user else {}
        province, city, district = loc.get("province"), loc.get("city"), loc.get("district")

    place = " ".join([x for x in (province, city, district) if x])
    detail = f"{district or city or '当前位置'} · 3 公里范围内" if place else None
    day = (date or today())[:10]

    # 自动清理超过有效期的缓存条目（内部 10 分钟节流，避免每次请求都全表扫描）
    await cache_store.purge_expired(db)

    # 1) 天气数据（3 小时缓存，命中则完全不调用上游天气源）
    cached, weather_hit = await cache_store.get_weather(
        db,
        province=province,
        city=city,
        district=district,
        place=place,
        detail=detail,
        day=day,
    )
    w = deepcopy(cached)
    # 地点与更新时间属于「本次请求」的信息，按当前入参重算，缓存只固化天气数值
    fresh = ai.build_weather(place, detail, day)["weather"]
    w.update(place=fresh["place"], placeDetail=fresh["placeDetail"], updated=fresh["updated"])

    # 2) 鱼情分析（3 小时缓存，键为全部查询参数的哈希，命中则完全不调用大模型）
    fish_params = {
        "province": province or "",
        "city": city or "",
        "district": district or "",
        "date": day,
        "place": place,
    }
    fish, fish_hit = await cache_store.get_fish(
        db, params=fish_params, weather=w, day_label=_day_label(day)
    )

    data = {
        "weather": w,
        "verdict": fish.get("verdict") or {},
        "slots": fish.get("slots") or [],
        "aiGenerated": fish.get("aiGenerated", False),
        "query": {"lat": lat, "lng": lng, "date": date},
    }
    cache_hit = weather_hit and fish_hit

    # 落一条天气查询日志，供管理后台查看 / 重新生成（失败不影响主流程）
    try:
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
