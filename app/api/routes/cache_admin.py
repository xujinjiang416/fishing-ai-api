"""cache_admin.py — 管理后台：天气缓存全生命周期管理（/admin/cache/*）

功能：
- 列表分页 + 多条件筛选（时间范围 / 城市 / 状态 / 数据来源）
- 详情查看完整缓存内容
- 手动刷新（单条 / 批量）、手动删除（单条 / 批量）
- 过期规则配置（城市等级 / 数据精度差异化 TTL）
- 统计看板（总数 / 占用空间 / 命中率 / 近 7 天趋势 / 即将过期）
- 高危操作（刷新 / 删除 / 规则修改）仅超级管理员，操作全程留痕
"""

import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_current_admin, get_db, get_super_admin
from ...core.response import fail, ok
from ...core.utils import today
from ...models import Admin, WeatherCache, WeatherLog
from ...schemas.admin import CacheBatchIn, CacheRulesIn
from ...services import ai, cache_policy, oplog

router = APIRouter(prefix="/admin/cache", tags=["缓存管理"])


def _page_args(page: int, page_size: int) -> tuple[int, int, int]:
    page = max(1, page or 1)
    page_size = min(max(1, page_size or 10), 100)
    return page, page_size, (page - 1) * page_size


def _res_404(name: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{name}不存在")


def _admin_ip(request: Request | None) -> str | None:
    try:
        return request.client.host if (request and request.client) else None
    except Exception:
        return None


def _day_label(day: str) -> str:
    try:
        delta = (dt.date.fromisoformat(day) - dt.date.fromisoformat(today())).days
    except ValueError:
        return day
    return {0: "今天", 1: "明天", 2: "后天"}.get(delta, day)


def _place_parts(data: dict) -> tuple[str | None, str | None, str | None]:
    """从快照 payload 解析省/市/区（依据 weather.place 的空格拼接）。"""
    place = str(((data or {}).get("weather") or {}).get("place") or "")
    parts = [p for p in place.split() if p]
    while len(parts) < 3:
        parts.append("")
    return parts[0] or None, parts[1] or None, parts[2] or None


def _fmt(ts) -> str | None:
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None


def _cache_out(row: WeatherCache) -> dict:
    now = dt.datetime.now()
    expired = bool(row.expire_at) and row.expire_at < now
    w = (row.payload or {}).get("weather") or {}
    return {
        "id": row.id,
        "cacheKey": f"{row.region}/{row.day}",
        "region": row.region,
        "day": row.day,
        "source": row.source,
        "createdAt": _fmt(row.created_at),
        "expireAt": _fmt(row.expire_at),
        "lastHitAt": _fmt(row.last_hit_at),
        "sizeBytes": row.size_bytes or 0,
        "sizeKb": round((row.size_bytes or 0) / 1024, 2),
        "hitCount": row.hit_count or 0,
        "status": "expired" if expired else "active",
        "condition": w.get("condition"),
        "temp": w.get("temp"),
    }


# ---------------- 列表 / 详情 ----------------
@router.get("", summary="缓存列表（分页 + 时间/城市/状态/来源筛选）")
async def list_cache(
    keyword: str | None = None,
    status: str | None = None,
    source: str | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(WeatherCache)

    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(WeatherCache.region.like(k))
    if source and source != "all":
        stmt = stmt.where(WeatherCache.source == source)
    if startDate:
        stmt = stmt.where(WeatherCache.created_at >= dt.datetime.fromisoformat(startDate[:10]))
    if endDate:
        stmt = stmt.where(
            WeatherCache.created_at < dt.datetime.fromisoformat(endDate[:10]) + dt.timedelta(days=1)
        )
    now = dt.datetime.now()
    if status == "active":
        stmt = stmt.where(or_(WeatherCache.expire_at.is_(None), WeatherCache.expire_at > now))
    elif status == "expired":
        stmt = stmt.where(WeatherCache.expire_at.is_not(None), WeatherCache.expire_at <= now)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await db.scalars(stmt.order_by(WeatherCache.id.desc()).offset(offset).limit(page_size))
    ).all()

    return ok({"list": [_cache_out(r) for r in rows], "total": total, "page": page, "pageSize": page_size})


# ---------------- 统计看板 ----------------
@router.get("/stats", summary="缓存统计看板")
async def cache_stats(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    now = dt.datetime.now()
    total = await db.scalar(select(func.count()).select_from(WeatherCache)) or 0
    total_size = await db.scalar(select(func.coalesce(func.sum(WeatherCache.size_bytes), 0))) or 0
    expiring = (
        await db.scalar(
            select(func.count())
            .select_from(WeatherCache)
            .where(
                WeatherCache.expire_at.is_not(None),
                WeatherCache.expire_at > now,
                WeatherCache.expire_at <= now + dt.timedelta(hours=24),
            )
        )
        or 0
    )

    # 近 7 天命中趋势（基于 weather_logs.cache_hit）
    start_day = (now - dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    hit = [0] * 7
    total_ = [0] * 7
    rows = (
        await db.execute(
            select(WeatherLog.cache_hit, WeatherLog.created_at).where(WeatherLog.created_at >= start_day)
        )
    ).all()
    for is_hit, ts in rows:
        if not ts:
            continue
        idx = (ts.date() - start_day.date()).days
        if 0 <= idx < 7:
            total_[idx] += 1
            if is_hit:
                hit[idx] += 1

    today_hits = hit[-1]
    today_total = total_[-1]
    return ok(
        {
            "totalCount": total,
            "totalSizeBytes": int(total_size),
            "totalSizeKb": round((int(total_size) or 0) / 1024, 2),
            "todayHitRate": round(today_hits * 100 / today_total, 1) if today_total else 0.0,
            "expiringSoon": expiring,
            "trend": {
                "days": [(start_day + dt.timedelta(days=i)).strftime("%m-%d") for i in range(7)],
                "hits": hit,
                "totals": total_,
            },
        }
    )


# ---------------- 过期规则配置 ----------------
@router.get("/rules", summary="缓存过期规则")
async def get_rules(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    return ok(await cache_policy.get_rules(db))


@router.put("/rules", summary="保存缓存过期规则")
async def save_rules(
    payload: CacheRulesIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    rules = {"cityLevels": payload.cityLevels, "precisions": payload.precisions}
    await cache_policy.save_rules(db, rules)
    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_RULE_UPDATE,
        admin=admin,
        target=cache_policy.RULES_KEY,
        detail=json.dumps(rules, ensure_ascii=False),
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok(await cache_policy.get_rules(db))


# ---------------- 手动刷新 / 删除（超级管理员） ----------------
@router.post("/batch-refresh", summary="批量刷新缓存")
async def batch_refresh(
    payload: CacheBatchIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要刷新的缓存")
    rows = (await db.scalars(select(WeatherCache).where(WeatherCache.id.in_(ids)))).all()
    refreshed = 0
    for row in rows:
        province, city, district = _place_parts(row.payload or {})
        place = " ".join([x for x in (province, city, district) if x])
        detail = f"{district or city or '当前位置'} · 3 公里范围内"
        try:
            data, source = await ai.generate_weather_traced(place, detail, row.day)
            data = await ai.judge_weather(data, _day_label(row.day))
        except Exception:
            continue  # 单条失败跳过，不影响批量
        ttl = await cache_policy.ttl_for(db, province=province, city=city, district=district)
        row.payload = data
        row.source = source
        row.expire_at = dt.datetime.now() + dt.timedelta(seconds=ttl)
        row.size_bytes = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        row.hit_count = 0
        row.last_hit_at = None
        row.created_at = dt.datetime.now()
        refreshed += 1

    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_BATCH_REFRESH,
        admin=admin,
        target=",".join(str(i) for i in ids[:20]),
        detail=f"批量刷新缓存 {refreshed} 条",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok({"refreshed": refreshed})


@router.post("/batch-delete", summary="批量删除缓存")
async def batch_delete(
    payload: CacheBatchIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的缓存")
    rows = (await db.scalars(select(WeatherCache).where(WeatherCache.id.in_(ids)))).all()
    for row in rows:
        await db.delete(row)
    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_BATCH_DELETE,
        admin=admin,
        target=",".join(str(i) for i in ids[:20]),
        detail=f"批量删除缓存 {len(rows)} 条",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok({"deleted": len(rows)})


@router.post("/{cache_id}/refresh", summary="刷新单条缓存（拉取最新上游数据）")
async def refresh_cache(
    cache_id: int,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(WeatherCache, cache_id)
    if not row:
        raise _res_404("缓存")

    province, city, district = _place_parts(row.payload or {})
    place = " ".join([x for x in (province, city, district) if x])
    detail = f"{district or city or '当前位置'} · 3 公里范围内"

    data, source = await ai.generate_weather_traced(place, detail, row.day)
    data = await ai.judge_weather(data, _day_label(row.day))
    ttl = await cache_policy.ttl_for(db, province=province, city=city, district=district)

    row.payload = data
    row.source = source
    row.expire_at = dt.datetime.now() + dt.timedelta(seconds=ttl)
    row.size_bytes = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    row.hit_count = 0
    row.last_hit_at = None
    row.created_at = dt.datetime.now()

    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_REFRESH,
        admin=admin,
        target=str(row.id),
        detail=f"刷新缓存 {row.region}/{row.day}（来源 {source}）",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok(_cache_out(row), "缓存已刷新")


@router.delete("/{cache_id}", summary="删除单条缓存")
async def delete_cache(
    cache_id: int,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(WeatherCache, cache_id)
    if not row:
        raise _res_404("缓存")
    region, day = row.region, row.day
    await db.delete(row)
    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_DELETE,
        admin=admin,
        target=str(cache_id),
        detail=f"删除缓存 {region}/{day}",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok()


# ---------------- 详情（动态路由，置于最后避免与 /stats、/rules 等静态路由冲突） ----------------
@router.get("/{cache_id}", summary="缓存详情（完整天气数据）")
async def cache_detail(
    cache_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(WeatherCache, cache_id)
    if not row:
        raise _res_404("缓存")
    return ok({"cache": _cache_out(row), "payload": row.payload})