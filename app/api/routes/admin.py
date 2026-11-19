"""admin.py — 管理后台接口（/admin/*）

统一约定：
- 响应走 core.response 的 {code, data, msg}；
- 入参 / 出参 camelCase（schemas/admin.py），库内 snake_case；
- 除 /login、/logout 外全部需要管理员 token（core.deps.get_current_admin）。
"""

import csv
import io
import random
import time as _time
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_current_admin, get_db
from ...core.response import fail, ok
from ...core.security import create_admin_token, verify_password
from ...core.utils import add_days, now_text, today
from ...models import (
    Admin,
    Goods,
    MembershipOrder,
    OperationLog,
    Order,
    Plan,
    Spot,
    SpotComment,
    User,
    WeatherLog,
)
from ...schemas.admin import (
    AdminLoginIn,
    AdminUserIn,
    AdminUserUpdateIn,
    BatchDeleteCommentsIn,
    BatchIdsIn,
    GoodsIn,
    GoodsUpdateIn,
    MembershipAssignIn,
    PlanIn,
    PlanUpdateIn,
    PointsConfigIn,
    SpotIn,
    SpotUpdateIn,
    admin_comment_out,
    admin_out,
    admin_user_out,
    goods_admin_out,
    membership_order_admin_out,
    operation_log_out,
    plan_out,
    spot_admin_out,
    weather_log_out,
)
from ...schemas.schemas import mask_phone, order_out
from ...services import ai, config_service, oplog

router = APIRouter(prefix="/admin", tags=["管理后台"])

STATIC_DIR = Path(__file__).resolve().parents[3] / "static" / "uploads"
UPLOAD_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}


def _page_args(page: int, page_size: int) -> tuple[int, int, int]:
    page = max(1, page or 1)
    page_size = min(max(1, page_size or 10), 100)
    return page, page_size, (page - 1) * page_size


def _paged(items: list, total: int, page: int, page_size: int) -> dict:
    return ok({"list": items, "total": total, "page": page, "pageSize": page_size})


def _res_404(name: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{name}不存在")


def _rel_time(ts) -> str:
    """相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前"""
    if not ts:
        return ""
    mins = int((datetime.now() - ts).total_seconds() // 60)
    if mins < 1:
        return "刚刚"
    if mins < 60:
        return f"{mins} 分钟前"
    if mins < 60 * 24:
        return f"{mins // 60} 小时前"
    return f"{mins // (60 * 24)} 天前"


# ---------------- 登录 ----------------
@router.post("/login", summary="管理员登录")
async def login(payload: AdminLoginIn, db: AsyncSession = Depends(get_db)):
    admin = await db.scalar(select(Admin).where(Admin.username == payload.username.strip()))
    if not admin or not verify_password(payload.password, admin.password_hash):
        return fail(401, "账号或密码错误")
    if admin.status != "normal":
        return fail(401, "账号已被停用")
    return ok({"token": create_admin_token(admin.id), "admin": admin_out(admin)})


@router.post("/logout", summary="退出登录（无状态 token，仅前端清理）")
async def logout():
    return ok()


# ---------------- 数据看板 ----------------
@router.get("/overview", summary="数据看板聚合")
async def overview(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    today_date = date.today()
    days = [today_date - timedelta(days=6 - i) for i in range(7)]
    labels = [d.strftime("%m-%d") for d in days]
    start = datetime.combine(days[0], time.min)

    users_total = await db.scalar(select(func.count()).select_from(User)) or 0
    member_total = await db.scalar(
        select(func.count()).select_from(User).where(
            or_(User.is_member.is_(True), User.plan.notin_(["none"]))
        )
    ) or 0

    weather_rows = (
        await db.scalars(
            select(WeatherLog.created_at).where(WeatherLog.created_at >= start)
        )
    ).all()
    user_rows = (
        await db.scalars(select(User.created_at).where(User.created_at >= start))
    ).all()
    order_rows = (
        await db.scalars(
            select(Order.created_at, Order.pay_amount).where(Order.created_at >= start)
        )
    ).all()

    weather_trend = [0] * 7
    for ts in weather_rows:
        if ts:
            idx = (ts.date() - days[0]).days
            if 0 <= idx < 7:
                weather_trend[idx] += 1
    users_trend = [0] * 7
    for ts in user_rows:
        if ts:
            idx = (ts.date() - days[0]).days
            if 0 <= idx < 7:
                users_trend[idx] += 1
    revenue_trend = [0.0] * 7
    for ts, amount in order_rows:
        if ts:
            idx = (ts.date() - days[0]).days
            if 0 <= idx < 7:
                revenue_trend[idx] += round(amount or 0, 2)

    # 最近动态：从注册 / 订单 / 天气日志合成
    events: list[tuple[datetime, str, str]] = []
    for u in (await db.scalars(select(User).order_by(User.id.desc()).limit(6))).all():
        events.append((u.created_at, "user-plus", f"新用户「{u.nickname}」注册"))
    for o in (await db.scalars(select(Order).order_by(Order.id.desc()).limit(6))).all():
        events.append((o.created_at, "package", f"订单 {o.order_no} 已创建（{o.status}）"))
    for w in (await db.scalars(select(WeatherLog).order_by(WeatherLog.id.desc()).limit(6))).all():
        events.append((w.created_at, "cloud-sun", f"用户「{w.user}」查询了 {w.place} 的天气"))
    events.sort(key=lambda e: e[0] or datetime.min, reverse=True)
    activity = [
        {"icon": icon, "text": text, "time": _rel_time(ts)} for ts, icon, text in events[:6]
    ]

    # 最新订单
    rows = await db.execute(
        select(Order, User.nickname)
        .join(User, User.id == Order.user_id)
        .order_by(Order.id.desc())
        .limit(8)
    )
    orders = []
    for o, nick in rows.all():
        item = order_out(o)
        item["user"] = nick
        orders.append(item)

    return ok(
        {
            "kpi": {
                "users": users_total,
                "weather": sum(weather_trend),
                "members": member_total,
                "revenue": round(sum(revenue_trend), 2),
                "usersToday": users_trend[-1],
                "weatherYesterday": weather_trend[-2] if len(weather_trend) > 1 else 0,
                "revenueYesterday": revenue_trend[-2] if len(revenue_trend) > 1 else 0,
                "memberRate": round(member_total * 100 / users_total, 1) if users_total else 0,
            },
            "trend": {
                "days": labels,
                "weather": weather_trend,
                "users": users_trend,
                "revenue": revenue_trend,
            },
            "activity": activity,
            "orders": orders,
        }
    )


# ---------------- 用户管理 ----------------
@router.get("/users", summary="用户列表（关键词 / 会员 / 状态筛选 + 分页）")
async def list_users(
    keyword: str | None = None,
    plan: str | None = None,
    status: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(User)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(User.nickname.like(k), User.phone.like(k), User.uid.like(k)))
    if plan and plan != "all":
        stmt = stmt.where(
            or_(User.plan.is_(None), User.plan == "none") if plan == "none" else User.plan == plan
        )
    if status and status != "all":
        stmt = stmt.where(User.status == status)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (await db.scalars(
        stmt.order_by(User.id.desc()).offset(offset).limit(page_size)
    )).all()

    counts: dict[int, int] = {}
    ids = [u.id for u in rows]
    if ids:
        res = await db.execute(
            select(WeatherLog.user_id, func.count())
            .where(WeatherLog.user_id.in_(ids))
            .group_by(WeatherLog.user_id)
        )
        counts = dict(res.all())

    items = [admin_user_out(u, counts.get(u.id, 0)) for u in rows]
    for item in items:
        item["phone"] = mask_phone(item["phone"])
    return _paged(items, total, page, page_size)


@router.post("/users", summary="新增用户")
async def create_user(
    payload: AdminUserIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.phone:
        exists = await db.scalar(select(User.id).where(User.phone == payload.phone))
        if exists:
            return fail(1001, "该手机号已注册")
    for _ in range(10):
        uid = "u_" + "".join(random.choices("0123456789", k=5))
        if not await db.scalar(select(User.id).where(User.uid == uid)):
            break
    user = User(
        uid=uid,
        account=payload.phone,
        phone=payload.phone,
        nickname=payload.nickname,
        avatar_text=payload.nickname[:1],
        city=payload.city,
        status=payload.status or "normal",
        plan="none",
        plan_name="未开通",
        points=0,
        streak=0,
        joined=today(),
        last_active="刚刚",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return ok(admin_user_out(user))


@router.put("/users/{uid}", summary="编辑用户")
async def update_user(
    uid: str,
    payload: AdminUserUpdateIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.uid == uid))
    if not user:
        raise _res_404("用户")
    if payload.nickname:
        user.nickname = payload.nickname
        user.avatar_text = payload.nickname[:1]
    # 前端列表手机号已脱敏，带 * 视为未修改
    if payload.phone and "*" not in payload.phone:
        user.phone = payload.phone
    if payload.city is not None:
        user.city = payload.city
    if payload.status:
        user.status = payload.status
    await db.commit()
    await db.refresh(user)
    return ok(admin_user_out(user))


@router.put("/users/{uid}/membership", summary="分配 / 撤销会员")
async def assign_membership(
    uid: str,
    payload: MembershipAssignIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.uid == uid))
    if not user:
        raise _res_404("用户")
    if payload.planId == "none":
        user.plan = "none"
        user.plan_name = "未开通"
        user.plan_expire = None
        user.is_member = False
        user.member_expire = None
    else:
        plan = await db.get(Plan, payload.planId)
        if not plan:
            return fail(1001, "套餐不存在")
        expire = add_days(None, payload.durationDays)
        user.plan = plan.id
        user.plan_name = plan.name
        user.plan_expire = expire
        # 同步小程序端的会员状态（is_member / member_expire）
        user.is_member = True
        user.member_expire = expire
        plan.sales = (plan.sales or 0) + 1
    await db.commit()
    await db.refresh(user)
    return ok(admin_user_out(user))


@router.delete("/users/{uid}", summary="删除用户")
async def delete_user(
    uid: str, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    user = await db.scalar(select(User).where(User.uid == uid))
    if not user:
        raise _res_404("用户")
    await db.delete(user)
    await db.commit()
    return ok()


@router.post("/users/batch-delete", summary="批量删除用户")
async def batch_delete_users(
    payload: BatchIdsIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按 uid 批量删除用户。"""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的用户")
    rows = (await db.scalars(select(User).where(User.uid.in_(ids)))).all()
    for u in rows:
        await db.delete(u)
    await db.commit()
    return ok({"deleted": len(rows)})


# ---------------- 会员套餐 ----------------
@router.get("/plans", summary="套餐列表")
async def list_plans(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    rows = (await db.scalars(select(Plan).order_by(Plan.sort, Plan.id))).all()
    return ok([plan_out(p) for p in rows])


@router.post("/plans", summary="新增套餐")
async def create_plan(
    payload: PlanIn, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    max_sort = await db.scalar(select(func.max(Plan.sort))) or 0
    plan = Plan(
        id=f"p{int(_time.time())}",
        name=payload.name,
        duration=payload.duration,
        duration_label=f"{payload.duration} 天",
        price=payload.price,
        orig_price=payload.origPrice,
        icon="crown",
        tag=payload.tag or "新套餐",
        tag_type="primary",
        sales=0,
        status="off",
        sort=max_sort + 1,
        benefits=payload.benefits or [],
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return ok(plan_out(plan))


@router.put("/plans/{plan_id}", summary="编辑套餐（含上下架切换）")
async def update_plan(
    plan_id: str,
    payload: PlanUpdateIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise _res_404("套餐")
    if payload.name:
        plan.name = payload.name
    if payload.duration:
        plan.duration = payload.duration
        plan.duration_label = f"{payload.duration} 天"
    if payload.price is not None:
        plan.price = payload.price
    if payload.origPrice is not None:
        plan.orig_price = payload.origPrice
    if payload.tag is not None:
        plan.tag = payload.tag
    if payload.benefits is not None:
        plan.benefits = payload.benefits
    if payload.status in ("on", "off"):
        plan.status = payload.status
    await db.commit()
    await db.refresh(plan)
    return ok(plan_out(plan))


@router.delete("/plans/{plan_id}", summary="删除套餐")
async def delete_plan(
    plan_id: str, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise _res_404("套餐")
    await db.delete(plan)
    await db.commit()
    return ok()


@router.post("/plans/batch-delete", summary="批量删除套餐")
async def batch_delete_plans(
    payload: BatchIdsIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按 id 批量删除套餐。"""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的套餐")
    rows = (await db.scalars(select(Plan).where(Plan.id.in_(ids)))).all()
    for p in rows:
        await db.delete(p)
    await db.commit()
    return ok({"deleted": len(rows)})


# ---------------- 积分商城 ----------------
@router.get("/goods", summary="商品列表（关键词 / 分类 / 状态筛选 + 分页）")
async def list_goods(
    keyword: str | None = None,
    cat: str | None = None,
    status: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(Goods)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(Goods.name.like(k), Goods.id.like(k)))
    if cat and cat != "全部":
        stmt = stmt.where(Goods.cat == cat)
    if status and status != "all":
        stmt = stmt.where(Goods.status == status)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (await db.scalars(stmt.order_by(Goods.id.desc()).offset(offset).limit(page_size))).all()
    return _paged([goods_admin_out(g) for g in rows], total, page, page_size)


@router.post("/goods", summary="新增商品")
async def create_goods(
    payload: GoodsIn, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    goods = Goods(
        id=f"g{int(_time.time())}",
        name=payload.name,
        cat=payload.cat,
        price=payload.price,
        orig=payload.origPrice or payload.price,
        img=payload.img,
        sales="0",
        stock=payload.stock,
        points_enabled=payload.pointsEnabled,
        status=payload.status or "off",
        updated=today(),
    )
    db.add(goods)
    await db.commit()
    await db.refresh(goods)
    return ok(goods_admin_out(goods))


@router.put("/goods/{goods_id}", summary="编辑商品（含上下架切换）")
async def update_goods(
    goods_id: str,
    payload: GoodsUpdateIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    goods = await db.get(Goods, goods_id)
    if not goods:
        raise _res_404("商品")
    if payload.name:
        goods.name = payload.name
    if payload.cat:
        goods.cat = payload.cat
    if payload.price is not None:
        goods.price = payload.price
    if payload.origPrice is not None:
        goods.orig = payload.origPrice
    if payload.stock is not None:
        goods.stock = payload.stock
    if payload.pointsEnabled is not None:
        goods.points_enabled = payload.pointsEnabled
    if payload.status in ("on", "off"):
        goods.status = payload.status
    if payload.img is not None:
        goods.img = payload.img
    goods.updated = today()
    await db.commit()
    await db.refresh(goods)
    return ok(goods_admin_out(goods))


@router.delete("/goods/{goods_id}", summary="删除商品")
async def delete_goods(
    goods_id: str, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    goods = await db.get(Goods, goods_id)
    if not goods:
        raise _res_404("商品")
    await db.delete(goods)
    await db.commit()
    return ok()


@router.post("/goods/batch-delete", summary="批量删除商品")
async def batch_delete_goods(
    payload: BatchIdsIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按 id 批量删除商品。"""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的商品")
    rows = (await db.scalars(select(Goods).where(Goods.id.in_(ids)))).all()
    for g in rows:
        await db.delete(g)
    await db.commit()
    return ok({"deleted": len(rows)})


# ---------------- 天气数据 ----------------
@router.get("/weather-logs", summary="天气记录列表（关键词 / 来源筛选 + 分页）")
async def list_weather_logs(
    keyword: str | None = None,
    source: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(WeatherLog)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(WeatherLog.user.like(k), WeatherLog.place.like(k), WeatherLog.id.like(k))
        )
    if source and source != "全部":
        stmt = stmt.where(WeatherLog.source == source)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (await db.scalars(
        stmt.order_by(WeatherLog.created_at.desc(), WeatherLog.id.desc()).offset(offset).limit(page_size)
    )).all()
    return _paged([weather_log_out(w) for w in rows], total, page, page_size)


@router.post("/weather-logs/{log_id}/regenerate", summary="重新生成该条天气/鱼情结论")
async def regenerate_weather(
    log_id: str,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    log = await db.get(WeatherLog, log_id)
    if not log:
        raise _res_404("天气记录")
    data = await ai.generate_weather(log.place, log.place_detail, today())
    data = await ai.judge_weather(data, "今天")
    w, v = data["weather"], data["verdict"]
    log.score = int(v["score"])
    log.verdict = v["name"]
    log.level = v["level"]
    log.condition = w.get("condition")
    log.temp = w.get("temp")
    log.pressure = w.get("pressure")
    log.wind_diff = f"{w.get('windDir', '')}{w.get('windLevel', '')}"
    log.date = now_text()
    await db.commit()
    await db.refresh(log)
    return ok(weather_log_out(log))


@router.delete("/weather-logs/{log_id}", summary="删除天气记录")
async def delete_weather_log(
    log_id: str, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    log = await db.get(WeatherLog, log_id)
    if not log:
        raise _res_404("天气记录")
    await db.delete(log)
    await db.commit()
    return ok()


@router.post("/weather-logs/batch-delete", summary="批量删除天气记录")
async def batch_delete_weather_logs(
    payload: BatchIdsIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按 id 批量删除天气记录。"""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的天气记录")
    rows = (await db.scalars(select(WeatherLog).where(WeatherLog.id.in_(ids)))).all()
    for w in rows:
        await db.delete(w)
    await db.commit()
    return ok({"deleted": len(rows)})


# ---------------- 钓点管理 ----------------
@router.get("/spots", summary="钓点列表（关键词 / 状态筛选 + 分页）")
async def list_spots(
    keyword: str | None = None,
    status: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(Spot)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(Spot.name.like(k), Spot.region.like(k)))
    if status and status != "all":
        stmt = stmt.where(Spot.status == status)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (await db.scalars(
        stmt.order_by(Spot.created_at.is_(None), Spot.created_at.desc()).offset(offset).limit(page_size)
    )).all()
    return _paged([spot_admin_out(s) for s in rows], total, page, page_size)


@router.post("/spots", summary="新增钓点")
async def create_spot(
    payload: SpotIn, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    spot = Spot(
        # id 必须以 ai 开头（seed 清理只保留 ai 前缀，管理员钓点不会被误删）
        id=f"ai{uuid.uuid4().hex[:14]}",
        name=payload.name,
        region=payload.city,
        coord=payload.coords,
        dist=payload.dist,
        pay=payload.pay or "免费",
        fish=payload.fish or [],
        gallery=payload.gallery or [],
        cover=payload.cover or (payload.gallery[0] if payload.gallery else None),
        status=payload.status or "online",
        rating=5.0,
        comment_count=0,
        added_by=admin.name,
        refreshed_at=datetime.now(),
        created_at=datetime.now(),
    )
    db.add(spot)
    await db.commit()
    await db.refresh(spot)
    return ok(spot_admin_out(spot))


@router.put("/spots/{spot_id}", summary="编辑钓点（含上/下架切换）")
async def update_spot(
    spot_id: str,
    payload: SpotUpdateIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    spot = await db.get(Spot, spot_id)
    if not spot:
        raise _res_404("钓点")
    # 一旦管理员编辑，即视为人工维护的钓点（added_by 非空），AI 到期刷新不再覆盖
    if not spot.added_by:
        spot.added_by = admin.name
    if payload.name:
        spot.name = payload.name
    if payload.city is not None:
        spot.region = payload.city
    if payload.coords is not None:
        spot.coord = payload.coords
    if payload.dist is not None:
        spot.dist = payload.dist
    if payload.pay is not None:
        spot.pay = payload.pay
    if payload.fish is not None:
        spot.fish = payload.fish
    if payload.gallery is not None:
        spot.gallery = payload.gallery
    if payload.cover is not None:
        spot.cover = payload.cover
    if payload.status in ("online", "offline"):
        spot.status = payload.status
    await db.commit()
    await db.refresh(spot)
    return ok(spot_admin_out(spot))


@router.delete("/spots/{spot_id}", summary="删除钓点")
async def delete_spot(
    spot_id: str, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    spot = await db.get(Spot, spot_id)
    if not spot:
        raise _res_404("钓点")
    await db.delete(spot)
    await db.commit()
    return ok()


@router.post("/spots/batch-delete", summary="批量删除钓点")
async def batch_delete_spots(
    payload: BatchIdsIn,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按 id 批量删除钓点。"""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的钓点")
    rows = (await db.scalars(select(Spot).where(Spot.id.in_(ids)))).all()
    for s in rows:
        await db.delete(s)
    await db.commit()
    return ok({"deleted": len(rows)})


# ---------------- 图片上传 ----------------
@router.post("/upload", summary="上传图片（商品图 / 钓点图）")
async def upload(
    file: UploadFile = File(...),
    admin: Admin = Depends(get_current_admin),
):
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in UPLOAD_EXTS:
        return fail(1001, "仅支持 jpg/png/webp/gif 图片")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        return fail(1001, "图片不能超过 5MB")
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{'jpg' if ext == 'jpeg' else ext}"
    (STATIC_DIR / name).write_bytes(content)
    return ok({"url": f"/static/uploads/{name}"})


# ---------------- 辅助：取请求来源 IP（尽力而为） ----------------
def _admin_ip(request: Request | None) -> str | None:
    """获取调用方 IP，用于操作日志留痕；取不到时返回 None，不影响主流程。"""
    try:
        return request.client.host if (request and request.client) else None
    except Exception:
        return None


# ---------------- 系统设置 · 积分配置 ----------------
async def _config_payload(db: AsyncSession) -> dict:
    """组装积分配置的返回体（camelCase + 配置项元数据）。"""
    values = await config_service.config_values(db)
    return {
        "checkinMemberPoints": values.get("checkin_member_points", 5),
        "checkinNormalPoints": values.get("checkin_normal_points", 1),
        "adPoints": values.get("ad_points", 5),
        "adSeconds": values.get("ad_seconds", 30),
        "adLimitNormal": values.get("ad_limit_normal", 15),
        "adLimitMember": values.get("ad_limit_member", 20),
        "sensitiveWords": await config_service.get_str(db, "sensitive_words"),
        # 配置项元数据（名称 / 说明 / 取值范围），供前端渲染表单校验
        "defs": [
            {"key": k, "remark": d.remark, "min": d.min, "max": d.max}
            for k, d in config_service.CONFIG_DEFS.items()
            if d.kind == "int"
        ],
    }


@router.get("/config", summary="积分配置详情")
async def get_config(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    return ok(await _config_payload(db))


@router.put("/config", summary="保存积分配置（实时生效 + 记录操作日志）")
async def update_config(
    payload: PointsConfigIn,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """保存积分配置：写入 app_configs 表后立即生效（缓存同步失效）。

    记录操作日志（谁在什么时候把哪一项从 A 改成了 B），满足审计要求。
    """
    updates = {
        "checkin_member_points": payload.checkinMemberPoints,
        "checkin_normal_points": payload.checkinNormalPoints,
        "ad_points": payload.adPoints,
        "ad_seconds": payload.adSeconds,
        "ad_limit_normal": payload.adLimitNormal,
        "ad_limit_member": payload.adLimitMember,
        "sensitive_words": payload.sensitiveWords,
    }
    changes = await config_service.apply_updates(db, updates)

    if changes:
        detail = "；".join(
            f"{config_service.CONFIG_DEFS[k].remark}：{old} → {new}" for k, old, new in changes
        )
        oplog.add_log(
            db,
            action=oplog.ACTION_CONFIG_UPDATE,
            admin=admin,
            target=",".join(k for k, _, _ in changes),
            detail=detail,
            ip=_admin_ip(request),
        )
    await db.commit()  # 配置变更与操作日志同事务提交
    return ok(await _config_payload(db))


# ---------------- 内容管理 · 评论管理 ----------------
@router.get("/comments", summary="评论列表（关键词 / 钓点筛选 + 分页）")
async def list_comments(
    keyword: str | None = None,
    spotId: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """分页展示全平台评论：评论人信息 / 关联内容 / 发布时间 / 评论正文。"""
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(SpotComment)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(SpotComment.text.like(k), SpotComment.user_name.like(k)))
    if spotId:
        stmt = stmt.where(SpotComment.spot_id == spotId)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await db.scalars(
            stmt.order_by(SpotComment.created_at.desc(), SpotComment.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    ).all()

    # 批量取关联钓点名称，避免 N+1 查询
    names: dict[str, str] = {}
    spot_ids = {c.spot_id for c in rows if c.spot_id}
    if spot_ids:
        res = await db.execute(select(Spot.id, Spot.name).where(Spot.id.in_(spot_ids)))
        names = dict(res.all())

    return _paged([admin_comment_out(c, names.get(c.spot_id)) for c in rows], total, page, page_size)


@router.delete("/comments/{comment_id}", summary="删除单条评论（记录操作日志）")
async def delete_comment(
    comment_id: str,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除评论并同步钓点评论数；删除后小程序端拉取到的评论列表即不再包含该条。"""
    comment = await db.get(SpotComment, comment_id)
    if not comment:
        raise _res_404("评论")
    spot_id = comment.spot_id
    excerpt = (comment.text or "")[:50]

    await db.delete(comment)
    # 级联维护钓点评论计数（前端列表展示用），不低于 0
    spot = await db.get(Spot, spot_id)
    if spot and (spot.comment_count or 0) > 0:
        spot.comment_count -= 1

    oplog.add_log(
        db,
        action=oplog.ACTION_COMMENT_DELETE,
        admin=admin,
        target=comment_id,
        detail=f"删除评论（钓点 {spot_id}）：{excerpt}",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok()


@router.post("/comments/batch-delete", summary="批量删除评论（记录操作日志）")
async def batch_delete_comments(
    payload: BatchDeleteCommentsIn,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按 id 批量删除评论，并同步各钓点的评论计数。"""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        return fail(1001, "请选择要删除的评论")

    rows = (await db.scalars(select(SpotComment).where(SpotComment.id.in_(ids)))).all()
    if not rows:
        return fail(1001, "未找到可删除的评论")

    # 统计每个钓点被删掉的评论数量，用于修正计数
    decrement: dict[str, int] = {}
    for c in rows:
        await db.delete(c)
        if c.spot_id:
            decrement[c.spot_id] = decrement.get(c.spot_id, 0) + 1
    for sid, n in decrement.items():
        spot = await db.get(Spot, sid)
        if spot:
            spot.comment_count = max(0, (spot.comment_count or 0) - n)

    oplog.add_log(
        db,
        action=oplog.ACTION_COMMENT_BATCH_DELETE,
        admin=admin,
        target=",".join(ids[:20]),
        detail=f"批量删除评论 {len(rows)} 条（id：{','.join(ids[:20])}{'…' if len(ids) > 20 else ''}）",
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok({"deleted": len(rows)})


# ---------------- 系统设置 · 操作日志 ----------------
@router.get("/logs", summary="操作日志列表（关键词 / 动作类型筛选 + 分页）")
async def list_logs(
    keyword: str | None = None,
    action: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """全链路操作日志查询：支持按动作类型与关键词（详情 / 管理员 / 对象）过滤。"""
    page, page_size, offset = _page_args(page, pageSize)
    stmt = select(OperationLog)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                OperationLog.detail.like(k),
                OperationLog.admin_name.like(k),
                OperationLog.target.like(k),
                OperationLog.action.like(k),
            )
        )
    if action and action != "all":
        stmt = stmt.where(OperationLog.action == action)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await db.scalars(
            stmt.order_by(OperationLog.id.desc()).offset(offset).limit(page_size)
        )
    ).all()
    return _paged([operation_log_out(x) for x in rows], total, page, page_size)


@router.get("/logs/export", summary="操作日志导出（CSV）")
async def export_logs(
    keyword: str | None = None,
    action: str | None = None,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """导出操作日志为 CSV（供本地留档 / 审计）。"""
    stmt = select(OperationLog)
    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                OperationLog.detail.like(k),
                OperationLog.admin_name.like(k),
                OperationLog.target.like(k),
                OperationLog.action.like(k),
            )
        )
    if action and action != "all":
        stmt = stmt.where(OperationLog.action == action)

    rows = (await db.scalars(stmt.order_by(OperationLog.id.desc()).limit(5000))).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "操作人", "操作类型", "操作对象", "操作详情", "来源IP", "操作时间"])
    for x in rows:
        writer.writerow(
            [
                x.id,
                x.admin_name,
                x.action,
                x.target or "",
                (x.detail or "").replace("\n", " "),
                x.ip or "",
                x.created_at.strftime("%Y-%m-%d %H:%M:%S") if x.created_at else "",
            ]
        )
    content = "\ufeff" + buf.getvalue()  # BOM：Excel 打开不乱码
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=operation_logs.csv"},
    )


# ---------------- 会员套餐 · 购买数据统计 ----------------
@router.get("/membership-stats", summary="会员套餐购买数据统计")
async def membership_stats(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    """统计会员套餐的购买情况：总单量 / 总金额 / 各套餐分布 / 近 7 日趋势 / 最近购买记录。"""
    total_orders = await db.scalar(select(func.count()).select_from(MembershipOrder)) or 0
    total_amount = await db.scalar(select(func.coalesce(func.sum(MembershipOrder.amount), 0))) or 0

    # 各套餐购买量 / 金额
    grp = await db.execute(
        select(
            MembershipOrder.plan,
            func.count(),
            func.coalesce(func.sum(MembershipOrder.amount), 0),
        ).group_by(MembershipOrder.plan)
    )
    plans = (await db.scalars(select(Plan))).all()
    name_map = {p.id: p.name for p in plans}
    sales_map = {p.id: (p.sales or 0) for p in plans}
    by_plan = [
        {
            "plan": pid,
            "name": name_map.get(pid, pid),
            "count": cnt,
            "amount": round(float(amt or 0), 2),
            "sales": sales_map.get(pid, 0),
        }
        for pid, cnt, amt in grp.all()
    ]

    # 近 7 日下单趋势
    today_date = date.today()
    days = [today_date - timedelta(days=6 - i) for i in range(7)]
    start = datetime.combine(days[0], time.min)
    orders = (
        await db.scalars(select(MembershipOrder).where(MembershipOrder.created_at >= start))
    ).all()
    counts = [0] * 7
    amounts = [0.0] * 7
    for o in orders:
        if not o.created_at:
            continue
        idx = (o.created_at.date() - days[0]).days
        if 0 <= idx < 7:
            counts[idx] += 1
            amounts[idx] += round(o.amount or 0, 2)

    # 最近购买记录（含用户昵称）
    recent_rows = await db.execute(
        select(MembershipOrder, User.nickname)
        .join(User, User.id == MembershipOrder.user_id)
        .order_by(MembershipOrder.id.desc())
        .limit(10)
    )
    recent = [membership_order_admin_out(o, nick) for o, nick in recent_rows.all()]

    return ok(
        {
            "totalOrders": total_orders,
            "totalAmount": round(float(total_amount or 0), 2),
            "byPlan": by_plan,
            "trend": {
                "days": [d.strftime("%m-%d") for d in days],
                "counts": counts,
                "amounts": [round(a, 2) for a in amounts],
            },
            "recent": recent,
        }
    )