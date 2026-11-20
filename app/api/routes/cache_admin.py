"""cache_admin.py — 管理后台：天气 / 鱼情缓存全生命周期管理（/admin/cache/*）

功能：
- 天气数据缓存：列表分页 + 多条件筛选（时间范围 / 地址 / 状态 / 来源）
- 鱼情分析缓存：列表分页 + 多条件筛选（时间范围 / 参数 / 状态 / 来源）
- 详情查看完整缓存内容
- 手动刷新天气（单条 / 批量）、手动删除（天气 / 鱼情，单条 / 批量）
- 缓存有效期规则配置（天气 / 鱼情，默认 3 小时）
- 统计看板（总数 / 占用空间 / 命中率 / 近 7 天趋势 / 即将过期）
- 高危操作（刷新 / 删除 / 规则修改）仅超级管理员，操作全程留痕

鉴权要求：所有接口均需管理后台 token（Authorization: Bearer <admin token>）；
刷新 / 删除 / 规则保存等高危操作仅「超级管理员」可执行。
依赖服务：MySQL（weather_cache / fish_cache / weather_logs 表）、services.cache_policy（TTL 规则）、
services.cache_store（重建缓存）、services.oplog（操作留痕）。
注意事项：`GET /{cache_id}` 为动态路由，必须注册在所有静态子路径之后，
否则会抢先匹配 /stats、/rules、/fish 等路径。
"""

import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_current_admin, get_db, get_super_admin
from ...core.response import fail, ok
from ...models import Admin, FishCache, WeatherCache, WeatherLog
from ...schemas.admin import CacheBatchIn, CacheRulesIn
from ...services import cache_policy, cache_store, oplog

router = APIRouter(prefix="/admin/cache", tags=["缓存管理"])


def _page_args(page: int, page_size: int) -> tuple[int, int, int]:
    """规范化分页参数：页码最小 1；每页条数限制在 1~100（缺省 10）；返回 (页码, 条数, 偏移量)。"""
    page = max(1, page or 1)
    page_size = min(max(1, page_size or 10), 100)
    return page, page_size, (page - 1) * page_size


def _res_404(name: str) -> HTTPException:
    """构造统一的 404 异常（缓存记录不存在）。"""
    return HTTPException(status_code=404, detail=f"{name}不存在")


def _admin_ip(request: Request | None) -> str | None:
    """取操作来源 IP 写入操作日志；取不到时返回 None，不影响主流程。"""
    try:
        return request.client.host if (request and request.client) else None
    except Exception:
        return None


def _fmt(ts) -> str | None:
    """datetime 格式化为后台展示用 `YYYY-MM-DD HH:MM:SS`；空值返回 None。"""
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None


def _status_of(expire_at, now: dt.datetime) -> str:
    """由过期时间推导缓存状态：expire_at 为空视为永不过期（active），早于当前时间即 expired。"""
    return "expired" if (expire_at is not None and expire_at < now) else "active"


def _cache_out(row: WeatherCache) -> dict:
    """天气缓存条目（对外结构，供后台列表 / 详情）。"""
    now = dt.datetime.now()
    w = (row.payload or {}).get("weather") or {}
    return {
        "type": "weather",
        "id": row.id,
        "cacheKey": row.cache_key or f"{row.region}|{row.day}",
        "region": row.region,
        "day": row.day,
        "source": row.source,
        "createdAt": _fmt(row.created_at),
        "expireAt": _fmt(row.expire_at),
        "lastHitAt": _fmt(row.last_hit_at),
        "sizeBytes": row.size_bytes or 0,
        "sizeKb": round((row.size_bytes or 0) / 1024, 2),
        "hitCount": row.hit_count or 0,
        "status": _status_of(row.expire_at, now),
        "condition": w.get("condition"),
        "temp": w.get("temp"),
    }


def _fish_out(row: FishCache) -> dict:
    """鱼情缓存条目（对外结构，供后台列表 / 详情）。"""
    now = dt.datetime.now()
    payload = row.payload or {}
    params = row.params or {}
    v = payload.get("verdict") or {}
    return {
        "type": "fish",
        "id": row.id,
        "cacheKey": row.cache_key,
        "region": row.region or params.get("place") or "默认",
        "day": params.get("date"),
        "params": params,
        "source": row.source,
        "createdAt": _fmt(row.created_at),
        "expireAt": _fmt(row.expire_at),
        "lastHitAt": _fmt(row.last_hit_at),
        "sizeBytes": row.size_bytes or 0,
        "sizeKb": round((row.size_bytes or 0) / 1024, 2),
        "hitCount": row.hit_count or 0,
        "status": _status_of(row.expire_at, now),
        "score": v.get("score"),
        "verdict": v.get("name"),
        "level": v.get("level"),
        "aiGenerated": bool(payload.get("aiGenerated")),
    }


# ---------------- 天气缓存：列表 / 详情 ----------------
@router.get("", summary="天气缓存列表（分页 + 时间/地址/状态/来源筛选）")
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
    """天气缓存列表（后台）。

    方法与路径：GET /admin/cache（需登录管理员）。
    查询参数：keyword 模糊匹配地区 / 缓存键；status=active|expired 按过期时间过滤（其他值不过滤）；
    source 来源筛选（"all" 视为不筛选）；startDate / endDate 按创建日期闭区间（仅取前 10 位日期）；
    page 从 1 开始；pageSize 1~100（默认 10）。
    响应：{ list: 天气缓存摘要[], total, page, pageSize }，按 id 倒序。
    """
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(WeatherCache)

    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(WeatherCache.region.like(k), WeatherCache.cache_key.like(k))
        )
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


# ---------------- 鱼情缓存：列表 / 详情 ----------------
@router.get("/fish", summary="鱼情缓存列表（分页 + 时间/参数/状态/来源筛选）")
async def list_fish_cache(
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
    """鱼情缓存列表（后台）。

    方法与路径：GET /admin/cache/fish（需登录管理员）。
    查询参数与天气列表一致：keyword / status / source / startDate / endDate / page / pageSize。
    响应：{ list: 鱼情缓存摘要[], total, page, pageSize }，按 id 倒序。
    """
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(FishCache)

    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(FishCache.region.like(k), FishCache.cache_key.like(k))
        )
    if source and source != "all":
        stmt = stmt.where(FishCache.source == source)
    if startDate:
        stmt = stmt.where(FishCache.created_at >= dt.datetime.fromisoformat(startDate[:10]))
    if endDate:
        stmt = stmt.where(
            FishCache.created_at < dt.datetime.fromisoformat(endDate[:10]) + dt.timedelta(days=1)
        )
    now = dt.datetime.now()
    if status == "active":
        stmt = stmt.where(or_(FishCache.expire_at.is_(None), FishCache.expire_at > now))
    elif status == "expired":
        stmt = stmt.where(FishCache.expire_at.is_not(None), FishCache.expire_at <= now)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await db.scalars(stmt.order_by(FishCache.id.desc()).offset(offset).limit(page_size))
    ).all()

    return ok({"list": [_fish_out(r) for r in rows], "total": total, "page": page, "pageSize": page_size})


# ---------------- 统计看板 ----------------
@router.get("/stats", summary="缓存统计看板（天气 + 鱼情）")
async def cache_stats(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    """缓存统计看板。

    方法与路径：GET /admin/cache/stats（需登录管理员）。
    响应：totalCount / weatherCount / fishCount、占用空间（字节与 KB）、今日命中率、
    24 小时内即将过期数量，以及近 7 天命中趋势（基于 weather_logs.cache_hit，缺失日期补 0）。
    说明：命中率 = 今日命中条数 / 今日查询总数，今日无查询时记 0。
    """
    now = dt.datetime.now()
    weather_total = await db.scalar(select(func.count()).select_from(WeatherCache)) or 0
    fish_total = await db.scalar(select(func.count()).select_from(FishCache)) or 0
    weather_size = (
        await db.scalar(select(func.coalesce(func.sum(WeatherCache.size_bytes), 0))) or 0
    )
    fish_size = await db.scalar(select(func.coalesce(func.sum(FishCache.size_bytes), 0))) or 0

    expiring_weather = (
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
    expiring_fish = (
        await db.scalar(
            select(func.count())
            .select_from(FishCache)
            .where(
                FishCache.expire_at.is_not(None),
                FishCache.expire_at > now,
                FishCache.expire_at <= now + dt.timedelta(hours=24),
            )
        )
        or 0
    )

    total = int(weather_total) + int(fish_total)
    total_size = int(weather_size) + int(fish_size)

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
            "weatherCount": int(weather_total),
            "fishCount": int(fish_total),
            "totalSizeBytes": total_size,
            "totalSizeKb": round(total_size / 1024, 2),
            "todayHitRate": round(today_hits * 100 / today_total, 1) if today_total else 0.0,
            "expiringSoon": int(expiring_weather) + int(expiring_fish),
            "trend": {
                "days": [(start_day + dt.timedelta(days=i)).strftime("%m-%d") for i in range(7)],
                "hits": hit,
                "totals": total_,
            },
        }
    )


# ---------------- 缓存有效期规则 ----------------
@router.get("/rules", summary="缓存有效期规则（天气 / 鱼情，秒）")
async def get_rules(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    """读取缓存有效期规则。

    方法与路径：GET /admin/cache/rules（需登录管理员）。
    响应：{ weather: 秒, fish: 秒 }，默认各 10800 秒（3 小时），持久化在 app_configs。
    """
    return ok(await cache_policy.get_rules(db))


@router.put("/rules", summary="保存缓存有效期规则")
async def save_rules(
    payload: CacheRulesIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """保存缓存有效期规则（高危，仅超级管理员）。

    方法与路径：PUT /admin/cache/rules。
    请求参数 CacheRulesIn：weather / fish 为 TTL 秒数，最小 60，默认 10800。
    副作用：写入 app_configs 并记录操作日志（cache.rule.update）；保存后新的缓存读写
    立即按新 TTL 计算过期时间。
    响应：保存后的规则。
    """
    rules = {"weather": payload.weather, "fish": payload.fish}
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
@router.post("/batch-refresh", summary="批量刷新天气缓存")
async def batch_refresh(
    payload: CacheBatchIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """批量刷新天气缓存（高危，仅超级管理员）。

    方法与路径：POST /admin/cache/batch-refresh。
    请求参数 CacheBatchIn：ids 为天气缓存主键列表，空列表返回业务错误 1001。
    行为：逐条调用 cache_store.rebuild_weather 重新拉取上游天气；单条失败跳过、不中断整批。
    响应：{ refreshed: 成功条数 }；操作日志仅记录前 20 个 id。
    """
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要刷新的缓存")
    rows = (await db.scalars(select(WeatherCache).where(WeatherCache.id.in_(ids)))).all()
    refreshed = 0
    for row in rows:
        try:
            await cache_store.rebuild_weather(db, row)
            refreshed += 1
        except Exception:  # noqa: BLE001
            continue  # 单条失败跳过，不影响批量

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


@router.post("/batch-delete", summary="批量删除天气缓存")
async def batch_delete(
    payload: CacheBatchIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """批量删除天气缓存（高危，仅超级管理员）。

    方法与路径：POST /admin/cache/batch-delete。
    请求参数 CacheBatchIn：ids 为天气缓存主键列表，空列表返回业务错误 1001。
    行为：仅删除存在的记录，不存在的 id 静默忽略；响应 { deleted: 实际删除条数 }。
    """
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


@router.post("/fish/batch-delete", summary="批量删除鱼情缓存")
async def batch_delete_fish(
    payload: CacheBatchIn,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """批量删除鱼情缓存（高危，仅超级管理员）。

    方法与路径：POST /admin/cache/fish/batch-delete。
    请求参数 CacheBatchIn：ids 为鱼情缓存主键列表，空列表返回业务错误 1001。
    行为：仅删除存在的记录，响应 { deleted: 实际删除条数 }。
    """
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的缓存")
    rows = (await db.scalars(select(FishCache).where(FishCache.id.in_(ids)))).all()
    for row in rows:
        await db.delete(row)
    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_BATCH_DELETE,
        admin=admin,
        target=",".join(str(i) for i in ids[:20]),
        detail=f"批量删除鱼情缓存 {len(rows)} 条",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok({"deleted": len(rows)})


@router.post("/{cache_id}/refresh", summary="刷新单条天气缓存（拉取最新上游数据）")
async def refresh_cache(
    cache_id: int,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """刷新单条天气缓存（高危，仅超级管理员）。

    方法与路径：POST /admin/cache/{cache_id}/refresh。
    行为：按该条缓存的地点 / 日期重新请求上游天气，重置 payload、来源与过期时间（按当前 TTL）。
    响应：{ cache: 刷新后的摘要 }；缓存不存在返回 404；上游异常向外抛出（由全局异常处理）。
    """
    row = await db.get(WeatherCache, cache_id)
    if not row:
        raise _res_404("缓存")

    source = await cache_store.rebuild_weather(db, row)

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


@router.delete("/{cache_id}", summary="删除单条天气缓存")
async def delete_cache(
    cache_id: int,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除单条天气缓存（高危，仅超级管理员）。

    方法与路径：DELETE /admin/cache/{cache_id}。
    响应：成功返回空 data；缓存不存在返回 404；操作日志记录被删缓存的地区与日期。
    """
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
@router.get("/fish/{fish_id}", summary="鱼情缓存详情（完整分析结果）")
async def fish_cache_detail(
    fish_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """鱼情缓存详情（完整分析结果）。

    方法与路径：GET /admin/cache/fish/{fish_id}（需登录管理员）。
    响应：{ cache: 摘要, payload: 大模型 / 规则引擎原始结论, params: 生成时的查询参数 }；
    不存在返回 404。
    """
    row = await db.get(FishCache, fish_id)
    if not row:
        raise _res_404("缓存")
    return ok({"cache": _fish_out(row), "payload": row.payload, "params": row.params})


@router.delete("/fish/{fish_id}", summary="删除单条鱼情缓存")
async def delete_fish_cache(
    fish_id: int,
    request: Request,
    admin: Admin = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除单条鱼情缓存（高危，仅超级管理员）。

    方法与路径：DELETE /admin/cache/fish/{fish_id}。
    响应：成功返回空 data；不存在返回 404；操作日志记录被删缓存的缓存键。
    """
    row = await db.get(FishCache, fish_id)
    if not row:
        raise _res_404("缓存")
    key = row.cache_key
    await db.delete(row)
    oplog.add_log(
        db,
        action=oplog.ACTION_CACHE_DELETE,
        admin=admin,
        target=str(fish_id),
        detail=f"删除鱼情缓存 {key}",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok()


@router.get("/{cache_id}", summary="天气缓存详情（完整天气数据）")
async def cache_detail(
    cache_id: int,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """天气缓存详情（完整天气数据）。

    方法与路径：GET /admin/cache/{cache_id}（需登录管理员）。
    响应：{ cache: 摘要, payload: 上游天气原始数据 }；不存在返回 404。
    注意：本动态路由置于文件末尾注册，避免与 /stats、/rules、/fish 等静态路由冲突。
    """
    row = await db.get(WeatherCache, cache_id)
    if not row:
        raise _res_404("缓存")
    return ok({"cache": _cache_out(row), "payload": row.payload})
