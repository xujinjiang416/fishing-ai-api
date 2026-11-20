"""admin.py — 管理后台接口（/admin/*）

职责：为管理后台（Web 端）提供全部 HTTP 接口，覆盖数据看板、用户管理、会员套餐、
积分商城商品、天气记录、钓点、评论、积分配置、操作日志、会员购买统计与订单管理。

统一约定：
- 响应走 core.response 的 {code, data, msg}，code = 0 表示成功；
- 入参 / 出参 camelCase（schemas/admin.py），库内 snake_case；
- 除 /login、/logout 外全部需要管理员 token（core.deps.get_current_admin）：
  从 Authorization: Bearer <admin token> 解析管理员 id，并校验账号存在且 status == "normal"；
- 订单写操作（关闭 / 发货 / 改地址 / 备注 / 退款 / 完成 / 删除）额外做角色校验，
  只读角色见 ORDER_READONLY_ROLES 与 _require_order_write；
- 列表接口统一分页：page 从 1 开始，pageSize 默认 10、上限 100（见 _page_args）；
- 删除类接口均为物理删除（不做软删除）；批量删除入参统一为 {"ids": [...]}。

注意事项：
- 评论删除、积分配置修改、订单变更会写操作日志（services.oplog），与业务写入同事务提交；
- 服务层（config_service / oplog / add_points）只 add 不 commit，提交统一由本文件路由函数完成；
- 图片上传仅允许 jpg/jpeg/png/webp/gif，单文件不超过 5MB；
- 管理员编辑过的钓点会写入 added_by，视为人工维护，AI 到期刷新不再覆盖（见 update_spot）。
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
    OrderLog,
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
    OrderAddressIn,
    OrderRefundIn,
    OrderRemarkIn,
    OrderShipIn,
    PlanIn,
    PlanUpdateIn,
    PointsConfigIn,
    SpotIn,
    SpotUpdateIn,
    admin_comment_out,
    admin_order_detail_out,
    admin_order_out,
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
from .user import add_points

router = APIRouter(prefix="/admin", tags=["管理后台"])

# 图片上传落盘目录：<项目根>/static/uploads，对外以 /static/uploads/<文件名> 访问
STATIC_DIR = Path(__file__).resolve().parents[3] / "static" / "uploads"
# 允许上传的图片扩展名白名单（小写比对）
UPLOAD_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}


def _page_args(page: int, page_size: int) -> tuple[int, int, int]:
    """分页参数归一化：page 最小 1，page_size 限制在 1~100（默认 10），返回 (page, page_size, offset)。"""
    page = max(1, page or 1)
    page_size = min(max(1, page_size or 10), 100)
    return page, page_size, (page - 1) * page_size


def _paged(items: list, total: int, page: int, page_size: int) -> dict:
    """统一分页响应：data = {list, total, page, pageSize}。"""
    return ok({"list": items, "total": total, "page": page, "pageSize": page_size})


def _res_404(name: str) -> HTTPException:
    """构造资源不存在的 404 异常，detail 为「{name}不存在」。"""
    return HTTPException(status_code=404, detail=f"{name}不存在")


def _rel_time(ts) -> str:
    """相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前（用于看板最近动态展示）"""
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
# 接口：POST /admin/login — 管理员登录（无需 token）
# 入参：AdminLoginIn{username, password}；username 先 strip 再查库，密码按 PBKDF2-SHA256 校验 admin.password_hash。
# 响应：成功 {token, admin}（admin 为 admin_out 序列化）；失败返回业务码 401（账号或密码错误 / 账号已被停用）。
# 限制：token 为无状态签名串，服务端不保存会话，因此没有服务端登出。
@router.post("/login", summary="管理员登录")
async def login(payload: AdminLoginIn, db: AsyncSession = Depends(get_db)):
    admin = await db.scalar(select(Admin).where(Admin.username == payload.username.strip()))
    if not admin or not verify_password(payload.password, admin.password_hash):
        return fail(401, "账号或密码错误")
    if admin.status != "normal":
        return fail(401, "账号已被停用")
    return ok({"token": create_admin_token(admin.id), "admin": admin_out(admin)})


# 接口：POST /admin/logout — 退出登录（无需 token，无状态）
# 说明：后端不维护会话与黑名单，直接返回成功，由前端清理本地 token。
@router.post("/logout", summary="退出登录（无状态 token，仅前端清理）")
async def logout():
    return ok()


# ---------------- 数据看板 ----------------
# 接口：GET /admin/overview — 数据看板聚合（需管理员 token）
# 无请求参数；固定统计最近 7 个自然日（含今天）。
# 响应：{
#   kpi: {users, weather(7 日天气查询总数), members, revenue(7 日实付合计),
#         usersToday, weatherYesterday, revenueYesterday, memberRate(会员占比 %)},
#   trend: {days[7], weather[7], users[7], revenue[7]},
#   activity: [{icon, text, time}] 最多 6 条,
#   orders: 最新 8 条订单（order_out + user 昵称）
# }
# 说明：所有统计均为全量/窗口内实时聚合，未做缓存。
@router.get("/overview", summary="数据看板聚合")
async def overview(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    today_date = date.today()
    # 统计窗口：含今天在内的最近 7 个自然日，days[0] 为 6 天前
    days = [today_date - timedelta(days=6 - i) for i in range(7)]
    labels = [d.strftime("%m-%d") for d in days]
    start = datetime.combine(days[0], time.min)

    users_total = await db.scalar(select(func.count()).select_from(User)) or 0
    # 会员口径：is_member 为真，或 plan 不为 "none"（兼容只写入了其中一个字段的历史数据）
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
        await db.execute(
            select(Order.created_at, Order.pay_amount).where(Order.created_at >= start)
        )
    ).all()

    # 按自然日把记录落到 7 个桶：idx = (记录日期 - days[0]).days，越界丢弃
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
# 接口：GET /admin/users — 用户列表（需管理员 token）
# Query：keyword（模糊匹配昵称 / 手机号 / uid）、plan（"all" 不过滤；"none" 表示 plan 为空或 "none"；其余按套餐 id 精确匹配）、
#        status（"all" 不过滤；其余按 User.status 精确匹配）、page（默认 1）、pageSize（默认 10，上限 100）。
# 响应：分页 {list,total,page,pageSize}，列表项 = admin_user_out + weatherCount（该用户天气查询次数），手机号脱敏。
# 查询：按 id 倒序；总数与列表复用同一筛选条件（子查询计数）。
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


# 接口：POST /admin/users — 新增用户（需管理员 token）
# 入参：AdminUserIn{nickname(必填,≤32), phone(可选,格式校验), city(可选), status(默认 "normal")}。
# 限制：手机号已存在时返回业务码 1001；uid 由后端生成（"u_" + 5 位数字，最多重试 10 次）。
# 新用户默认：plan="none"、plan_name="未开通"、points=0、streak=0、joined=今天、last_active="刚刚"。
# 响应：admin_user_out（weatherCount 默认为 0）。
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


# 接口：PUT /admin/users/{uid} — 编辑用户（需管理员 token）
# 路径参数：uid。入参 AdminUserUpdateIn 各字段可选，仅传入的字段会被更新。
# 特殊处理：nickname 变更时同步 avatar_text=首字符；phone 含 "*" 视为前端脱敏回显，不更新。
# 响应：admin_user_out；用户不存在抛 404。
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


# 接口：PUT /admin/users/{uid}/membership — 分配 / 撤销会员（需管理员 token）
# 路径参数：uid。入参 MembershipAssignIn{planId, durationDays(默认 30，范围 1~3650)}。
# 分支：planId == "none" 表示撤销，清空 plan / plan_name / plan_expire / is_member / member_expire；
#       否则校验套餐存在，plan_expire 与 member_expire 同步为「今天 + durationDays 天」的日期字符串，并把套餐 sales +1。
# 响应：admin_user_out；套餐不存在返回业务码 1001，用户不存在抛 404。
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


# 接口：DELETE /admin/users/{uid} — 删除用户（需管理员 token，物理删除、不可恢复）
# 路径参数：uid；用户不存在抛 404；响应 data 为空。
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


# 接口：POST /admin/users/batch-delete — 批量删除用户（需管理员 token）
# 入参：BatchIdsIn{ids: [uid, ...]}；ids 为 uid 列表，空列表返回业务码 1001。
# 行为：按 uid 查询后逐条物理删除，不存在的 uid 自动忽略；响应 {"deleted": 实际删除条数}。
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
# 接口：GET /admin/plans — 套餐列表（需管理员 token）
# 无参数、不分页；返回全部套餐（含下架），按 sort 升序、id 升序；data 为 plan_out 数组。
@router.get("/plans", summary="套餐列表")
async def list_plans(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    rows = (await db.scalars(select(Plan).order_by(Plan.sort, Plan.id))).all()
    return ok([plan_out(p) for p in rows])


# 接口：POST /admin/plans — 新增套餐（需管理员 token）
# 入参：PlanIn{name(≤16), duration(1~3650 天), price(≥0), origPrice, tag, benefits[]}。
# 默认值：id = "p" + 秒级时间戳；icon="crown"；tag 缺省 "新套餐"；tag_type="primary"；sales=0；
#        status="off"（新增后默认下架，需再编辑上架）；sort = 当前最大 sort + 1。
# 响应：plan_out。
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


# 接口：PUT /admin/plans/{plan_id} — 编辑套餐 / 上下架（需管理员 token）
# 路径参数：plan_id。入参 PlanUpdateIn 各字段可选；duration 变更时同步 duration_label="{n} 天"。
# 状态机：status 仅接受 "on"（上架）/ "off"（下架），其他值忽略；响应 plan_out；套餐不存在抛 404。
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


# 接口：DELETE /admin/plans/{plan_id} — 删除套餐（需管理员 token，物理删除）
# 路径参数：plan_id；套餐不存在抛 404；不会回滚已产生的会员订单 / 用户会员状态。
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


# 接口：POST /admin/plans/batch-delete — 批量删除套餐（需管理员 token）
# 入参：BatchIdsIn{ids: [plan_id, ...]}；空列表返回业务码 1001；响应 {"deleted": 实际删除条数}。
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
# 接口：GET /admin/goods — 商品列表（需管理员 token）
# Query：keyword（模糊匹配商品名称 / id）、cat（"全部" 不过滤，其余精确匹配）、
#        status（"all" 不过滤，其余精确匹配）、page（默认 1）、pageSize（默认 10，上限 100）。
# 响应：分页 {list,total,page,pageSize}，列表项为 goods_admin_out；按 id 倒序。
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


# 接口：POST /admin/goods — 新增商品（需管理员 token）
# 入参：GoodsIn{name(≤64), cat, price(≥0), origPrice, stock(≥0, 默认 0), pointsEnabled(默认 true), status, img}。
# 默认值：id = "g" + 秒级时间戳；orig 缺省回退 price；sales="0"；status 取 payload.status（为空时回落 "off"）；updated=今天。
# 响应：goods_admin_out。
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


# 接口：PUT /admin/goods/{goods_id} — 编辑商品 / 上下架（需管理员 token）
# 路径参数：goods_id。入参 GoodsUpdateIn 各字段可选，仅更新传入项；status 仅接受 "on" / "off"。
# 业务：每次编辑都会把 updated 刷新为今天；响应 goods_admin_out；商品不存在抛 404。
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


# 接口：DELETE /admin/goods/{goods_id} — 删除商品（需管理员 token，物理删除）
# 路径参数：goods_id；商品不存在抛 404；已产生的订单保留订单内的商品名快照。
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


# 接口：POST /admin/goods/batch-delete — 批量删除商品（需管理员 token）
# 入参：BatchIdsIn{ids: [goods_id, ...]}；空列表返回业务码 1001；响应 {"deleted": 实际删除条数}。
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
# 接口：GET /admin/weather-logs — 天气查询记录列表（需管理员 token）
# Query：keyword（模糊匹配查询人 / 地点 / 记录 id）、source（"全部" 不过滤，其余精确匹配数据来源）、
#        page（默认 1）、pageSize（默认 10，上限 100）。
# 响应：分页 {list,total,page,pageSize}，列表项为 weather_log_out；按 created_at 倒序，同一时间再按 id 倒序。
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


# 接口：POST /admin/weather-logs/{log_id}/regenerate — 重新生成该条天气 / 鱼情结论（需管理员 token）
# 路径参数：log_id。流程：用记录中的 place / place_detail + 今天日期重新调用 ai.generate_weather，
#          再调用 ai.judge_weather("今天")，把 score / verdict / level / condition / temp / pressure / wind_diff 回写，
#          并把 date 更新为当前时间文本（now_text）。
# 响应：更新后的 weather_log_out；记录不存在抛 404。注意：source / cost 字段本次不改写。
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


# 接口：DELETE /admin/weather-logs/{log_id} — 删除天气记录（需管理员 token，物理删除）
# 路径参数：log_id；记录不存在抛 404；响应 data 为空。
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


# 接口：POST /admin/weather-logs/batch-delete — 批量删除天气记录（需管理员 token）
# 入参：BatchIdsIn{ids: [log_id, ...]}；空列表返回业务码 1001；响应 {"deleted": 实际删除条数}。
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
# 接口：GET /admin/spots — 钓点列表（需管理员 token）
# Query：keyword（模糊匹配钓点名称 / 所在区域）、status（"all" 不过滤，其余精确匹配 online/offline）、
#        page（默认 1）、pageSize（默认 10，上限 100）。
# 排序：先按 created_at 是否为空（NULL 排最后），再按 created_at 倒序。
# 响应：分页 {list,total,page,pageSize}，列表项为 spot_admin_out。
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


# 接口：POST /admin/spots — 新增钓点（需管理员 token）
# 入参：SpotIn{name(≤64), city, coords, dist(≥0, 默认 0), pay, fish[], gallery[], cover, status(默认 online)}。
# 默认值：id = "ai" + 14 位 uuid（前缀 ai 保证 seed 清理不会误删管理员钓点）；pay 缺省 "免费"；
#        cover 缺省取 gallery[0]；rating=5.0；comment_count=0；added_by=当前管理员名；refreshed_at/created_at=当前时间。
# 响应：spot_admin_out。
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


# 接口：PUT /admin/spots/{spot_id} — 编辑钓点 / 上下架（需管理员 token）
# 路径参数：spot_id。入参 SpotUpdateIn 各字段可选；status 仅接受 "online" / "offline"。
# 业务：若 added_by 为空，则写入当前管理员名，标记为人工维护（后续 AI 到期刷新不再覆盖）。
# 响应：spot_admin_out；钓点不存在抛 404。
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


# 接口：DELETE /admin/spots/{spot_id} — 删除钓点（需管理员 token，物理删除）
# 路径参数：spot_id；钓点不存在抛 404；响应 data 为空（关联评论按 ORM cascade / 外键 ON DELETE CASCADE 一并清理）。
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


# 接口：POST /admin/spots/batch-delete — 批量删除钓点（需管理员 token）
# 入参：BatchIdsIn{ids: [spot_id, ...]}；空列表返回业务码 1001；响应 {"deleted": 实际删除条数}。
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
# 接口：POST /admin/upload — 上传图片（需管理员 token），multipart/form-data 字段名 file。
# 限制：扩展名必须在 UPLOAD_EXTS 内；文件内容读取后校验大小 ≤ 5MB；jpeg 统一存为 .jpg，文件名用 uuid 避免覆盖。
# 落盘：STATIC_DIR（static/uploads）；响应 {"url": "/static/uploads/<文件名>"}，供商品图 / 钓点图直接引用。
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
    """组装积分配置的返回体（camelCase + 配置项元数据）。

    读取顺序：config_service 的强类型快照 → 缺省值兜底（会员签到 5 / 普通签到 1 / 广告 5 分 30 秒 /
    每日广告次数普通 15、会员 20）；defs 仅暴露 kind == "int" 的配置项，供前端渲染与校验。
    """
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


# 接口：GET /admin/config — 积分配置详情（需管理员 token）
# 无参数；响应 = _config_payload：{checkinMemberPoints, checkinNormalPoints, adPoints, adSeconds,
#          adLimitNormal, adLimitMember, sensitiveWords, defs[{key, remark, min, max}]}。
@router.get("/config", summary="积分配置详情")
async def get_config(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    return ok(await _config_payload(db))


# 接口：PUT /admin/config — 保存积分配置（需管理员 token）
# 入参：PointsConfigIn 各字段可选，None 表示本次不修改；sensitiveWords 为英文逗号分隔字符串，空串表示清空自定义词库。
# 行为：apply_updates 仅落库「有变化」的键；有变更时拼接「名称：旧值 → 新值」写入操作日志（含来源 IP），
#       配置变更与日志同一事务提交；无变更则不写日志。
# 响应：保存后的完整配置（结构同 GET /admin/config）。
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
# 接口：GET /admin/comments — 评论列表（需管理员 token）
# Query：keyword（模糊匹配评论正文 / 评论人昵称）、spotId（按钓点精确过滤）、page（默认 1）、pageSize（默认 10，上限 100）。
# 响应：分页 {list,total,page,pageSize}，列表项为 admin_comment_out（含关联钓点名称，已批量查询避免 N+1）。
# 排序：created_at 倒序，同一时间再按 id 倒序。
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


# 接口：DELETE /admin/comments/{comment_id} — 删除单条评论（需管理员 token）
# 路径参数：comment_id。行为：物理删除评论 → 关联钓点 comment_count 减 1（已为 0 则不变）→ 写操作日志（截取正文前 50 字）。
# 响应：data 为空；评论不存在抛 404。
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


# 接口：POST /admin/comments/batch-delete — 批量删除评论（需管理员 token）
# 入参：BatchDeleteCommentsIn{ids: [comment_id, ...]}；空列表 / 全部不存在均返回业务码 1001。
# 行为：按钓点汇总删除条数，comment_count = max(0, 原值 - n)；操作日志最多记录前 20 个 id。
# 响应：{"deleted": 实际删除条数}。
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
# 接口：GET /admin/logs — 操作日志列表（需管理员 token）
# Query：keyword（模糊匹配详情 / 操作人 / 操作对象 / 动作类型）、action（"all" 不过滤，其余精确匹配）、
#        page（默认 1）、pageSize（默认 10，上限 100）。
# 响应：分页 {list,total,page,pageSize}，列表项为 operation_log_out；按 id 倒序（最新在前）。
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


# 接口：GET /admin/logs/export — 操作日志导出 CSV（需管理员 token）
# Query：keyword / action（过滤规则与列表接口一致，但不分页）；固定最多导出 5000 条（按 id 倒序）。
# 响应：text/csv 流式下载，文件 operation_logs.csv；首行表头，内容含 UTF-8 BOM 以便 Excel 正常显示中文；
#       详情中的换行会被替换为空格。
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
# 接口：GET /admin/membership-stats — 会员套餐购买数据统计（需管理员 token）
# 无参数；响应：{
#   totalOrders, totalAmount（全部会员订单数与金额）,
#   byPlan: [{plan, name, count, amount, sales}]（按套餐聚合，name/sales 取自 plans 表）,
#   trend: {days[7], counts[7], amounts[7]}（近 7 个自然日）,
#   recent: 最近 10 条购买记录（membership_order_admin_out，含用户昵称）
# }
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


# ================= 订单管理 =================
# 本段提供订单统计、导出、列表、详情，以及关闭 / 发货 / 改地址 / 备注 / 退款 / 完成 / 删除等写入接口。
# 状态机（Order.status）：
#   待支付 → 已关闭（后台关闭）
#   待发货 → 已发货（后台发货，写物流信息）→ 已完成（后台确认完成）
#   已发货 / 待发货 → 退款中 → 已退款（全额）或 部分退款（部分），驳回则回到发货前状态
# 所有写接口在 get_current_admin 基础上再做角色校验（_require_order_write），并写订单日志 + 全局操作日志。
#
# 订单状态全集（与小程序 / 后台共用一套中文状态）
ORDER_STATUSES = ["待支付", "待发货", "已发货", "已完成", "已关闭", "退款中", "部分退款", "已退款"]
# 只读角色：可查看 / 导出，但不能执行订单操作（关闭 / 发货 / 退款等）；命中即 403
ORDER_READONLY_ROLES = ("只读管理员", "观察员", "审计员")

# 导出字段白名单：key -> 中文表头（前端按 key 自定义选择导出列）
ORDER_EXPORT_FIELDS: dict[str, str] = {
    "no": "订单编号",
    "user": "下单用户",
    "uid": "用户ID",
    "userPhone": "用户手机号",
    "goods": "商品",
    "qty": "数量",
    "total": "商品金额",
    "usePoints": "抵扣积分",
    "amount": "实付金额",
    "payMethod": "支付方式",
    "payStatus": "支付状态",
    "status": "订单状态",
    "deliveryType": "配送方式",
    "receiver": "收货人",
    "receiverPhone": "收货电话",
    "address": "收货地址",
    "logisticsCompany": "物流公司",
    "logisticsNo": "物流单号",
    "refundAmount": "退款金额",
    "remark": "备注",
    "createdAt": "下单时间",
    "paidAt": "支付时间",
    "shippedAt": "发货时间",
}


def _require_order_write(admin: Admin) -> None:
    """订单写操作权限校验：角色命中 ORDER_READONLY_ROLES 时抛 403「当前角色无订单操作权限」。

    仅在写接口（关闭 / 发货 / 改地址 / 备注 / 退款 / 完成 / 删除）调用，列表与导出不限制。
    """
    if (admin.role or "") in ORDER_READONLY_ROLES:
        raise HTTPException(status_code=403, detail="当前角色无订单操作权限")


def _parse_day(value: str, end: bool = False) -> datetime | None:
    """把 YYYY-MM-DD 解析成当天 00:00；end=True 时返回次日 00:00（用于闭区间）。

    只取前 10 个字符，兼容带时间的 ISO 字符串；解析失败返回 None，调用方据此忽略该时间条件。
    """
    try:
        d = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None
    return d + timedelta(days=1) if end else d


def _order_stmt(
    keyword: str | None = None,
    userKeyword: str | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
    payMethod: str | None = None,
    status: str | None = None,
    deliveryType: str | None = None,
    quick: str | None = None,
):
    """按筛选条件构建订单查询（联表用户，便于按昵称 / 手机号 / UID 过滤）。

    参数：
    - keyword：模糊匹配订单号 / 商品名；
    - userKeyword：模糊匹配用户昵称 / uid / 手机号；
    - startDate / endDate：YYYY-MM-DD，按下单时间做左闭右开区间（endDate 取次日 00:00）；
    - payMethod / deliveryType：精确匹配，"all" 表示不过滤；
    - status：支持英文逗号分隔的多状态（如 "待发货,已发货"），"all" 表示不过滤；
    - quick：快捷筛选 today / unpaid / unshipped / shipped / refunding / abnormal。
    返回未排序、未分页的 select(Order, User)；User 用 outerjoin，用户被删时订单仍可查询。
    """
    stmt = select(Order, User).outerjoin(User, User.id == Order.user_id)

    if keyword and keyword.strip():
        k = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(Order.order_no.like(k), Order.goods_name.like(k)))
    if userKeyword and userKeyword.strip():
        k = f"%{userKeyword.strip()}%"
        stmt = stmt.where(or_(User.nickname.like(k), User.uid.like(k), User.phone.like(k)))
    if startDate:
        start = _parse_day(startDate)
        if start:
            stmt = stmt.where(Order.created_at >= start)
    if endDate:
        end = _parse_day(endDate, end=True)
        if end:
            stmt = stmt.where(Order.created_at < end)
    if payMethod and payMethod != "all":
        stmt = stmt.where(Order.pay_method == payMethod)
    if deliveryType and deliveryType != "all":
        stmt = stmt.where(Order.delivery_type == deliveryType)
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            stmt = stmt.where(Order.status.in_(statuses))

    if quick == "today":
        stmt = stmt.where(Order.created_at >= datetime.combine(date.today(), time.min))
    elif quick == "unpaid":
        stmt = stmt.where(Order.status == "待支付")
    elif quick == "unshipped":
        stmt = stmt.where(Order.status == "待发货")
    elif quick == "shipped":
        stmt = stmt.where(Order.status == "已发货")
    elif quick == "refunding":
        stmt = stmt.where(or_(Order.status == "退款中", Order.refund_status == "processing"))
    elif quick == "abnormal":
        stmt = stmt.where(Order.abnormal.is_(True))

    return stmt


async def _goods_map(db: AsyncSession, orders: list[Order]) -> dict[str, Goods]:
    """批量取订单关联商品（用于图片 / 分类），避免 N+1 查询；返回 {goods_id: Goods}，缺失的商品不在结果中。"""
    ids = {o.goods_id for o in orders if o.goods_id}
    if not ids:
        return {}
    rows = (await db.scalars(select(Goods).where(Goods.id.in_(ids)))).all()
    return {g.id: g for g in rows}


def _order_log(
    db: AsyncSession,
    order_no: str,
    action: str,
    detail: str,
    admin: Admin | None,
    request: Request | None = None,
) -> None:
    """写订单操作日志 + 全局操作日志（不提交，由调用方统一 commit）。

    - OrderLog：按订单号记录动作 / 操作人 / 详情，用于订单详情时间线；
    - OperationLog：action 统一加 "order." 前缀（如 order.ship），target 为订单号；
    - 操作人取 admin.name，为空时记为「系统」/「系统管理员」。
    """
    operator = getattr(admin, "name", None) or "系统"
    db.add(OrderLog(order_no=order_no, action=action, operator=operator, detail=detail))
    oplog.add_log(
        db,
        action=f"order.{action}",
        admin=admin,
        target=order_no,
        detail=detail,
        ip=_admin_ip(request),
    )


# 接口：GET /admin/orders/stats — 订单统计概览（需管理员 token，所有角色可读）
# 无参数；响应：todayOrders / todayAmount（金额仅统计已支付及之后状态）/ pendingCount（待支付+待发货+退款中）/
#   totalOrders / counts{unpaid,unshipped,refunding,abnormal} / trend{days[7],counts[7],amounts[7]}。
# 说明：趋势按 created_at 落入最近 7 个自然日，金额同样只累加 paid_statuses 内的状态。
@router.get("/orders/stats", summary="订单统计概览（今日 / 待处理 / 近 7 日趋势）")
async def order_stats(
    admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    today_start = datetime.combine(date.today(), time.min)
    # 今日成交金额只统计已支付及之后的状态（排除待支付 / 已关闭）
    paid_statuses = ["待发货", "已发货", "已完成", "退款中", "部分退款", "已退款"]

    today_count = await db.scalar(
        select(func.count()).select_from(Order).where(Order.created_at >= today_start)
    ) or 0
    today_amount = await db.scalar(
        select(func.coalesce(func.sum(Order.pay_amount), 0)).where(
            Order.created_at >= today_start, Order.status.in_(paid_statuses)
        )
    ) or 0

    unpaid = await db.scalar(
        select(func.count()).select_from(Order).where(Order.status == "待支付")
    ) or 0
    unshipped = await db.scalar(
        select(func.count()).select_from(Order).where(Order.status == "待发货")
    ) or 0
    refunding = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(or_(Order.status == "退款中", Order.refund_status == "processing"))
    ) or 0
    abnormal = await db.scalar(
        select(func.count()).select_from(Order).where(Order.abnormal.is_(True))
    ) or 0
    total = await db.scalar(select(func.count()).select_from(Order)) or 0

    # 近 7 日趋势
    days = [date.today() - timedelta(days=6 - i) for i in range(7)]
    start = datetime.combine(days[0], time.min)
    rows = (
        await db.execute(
            select(Order.created_at, Order.pay_amount, Order.status).where(Order.created_at >= start)
        )
    ).all()
    counts = [0] * 7
    amounts = [0.0] * 7
    for ts, amount, st in rows:
        if not ts:
            continue
        idx = (ts.date() - days[0]).days
        if 0 <= idx < 7:
            counts[idx] += 1
            if st in paid_statuses:
                amounts[idx] += round(amount or 0, 2)

    return ok(
        {
            "todayOrders": today_count,
            "todayAmount": round(float(today_amount or 0), 2),
            "pendingCount": unpaid + unshipped + refunding,
            "totalOrders": total,
            "counts": {
                "unpaid": unpaid,
                "unshipped": unshipped,
                "refunding": refunding,
                "abnormal": abnormal,
            },
            "trend": {
                "days": [d.strftime("%m-%d") for d in days],
                "counts": counts,
                "amounts": [round(a, 2) for a in amounts],
            },
        }
    )


# 接口：GET /admin/orders/export — 订单导出 CSV（需管理员 token，所有角色可读）
# Query：与 GET /admin/orders 相同的筛选参数，另加 fields（英文逗号分隔的导出字段 key）。
# 限制：fields 仅接受 ORDER_EXPORT_FIELDS 白名单内的 key，为空或全部非法时导出全部字段；
#       最多导出 10000 条（按 created_at、id 倒序）。
# 响应：text/csv 流式下载，文件名 orders_YYYYMMDD_HHMMSS.csv，内容含 UTF-8 BOM 以兼容 Excel。
@router.get("/orders/export", summary="订单导出（Excel 可直接打开的 CSV，字段可选）")
async def export_orders(
    keyword: str | None = None,
    userKeyword: str | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
    payMethod: str | None = None,
    status: str | None = None,
    deliveryType: str | None = None,
    quick: str | None = None,
    fields: str | None = None,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """按当前筛选条件导出订单；fields 为逗号分隔的字段 key，缺省导出全部白名单字段。"""
    stmt = _order_stmt(
        keyword, userKeyword, startDate, endDate, payMethod, status, deliveryType, quick
    )
    rows = (
        await db.execute(stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(10000))
    ).all()
    orders = [o for o, _ in rows]
    goods_map = await _goods_map(db, orders)

    selected = [f.strip() for f in (fields or "").split(",") if f.strip() in ORDER_EXPORT_FIELDS]
    if not selected:
        selected = list(ORDER_EXPORT_FIELDS.keys())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([ORDER_EXPORT_FIELDS[k] for k in selected])
    for order, user in rows:
        item = admin_order_out(order, user, goods_map.get(order.goods_id))
        writer.writerow([item.get(k, "") for k in selected])

    content = "\ufeff" + buf.getvalue()  # BOM：Excel 打开中文不乱码
    filename = f"orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# 接口：GET /admin/orders — 订单列表（需管理员 token，所有角色可读）
# Query：keyword / userKeyword / startDate / endDate / payMethod / status / deliveryType / quick（含义见 _order_stmt），
#        page（默认 1）、pageSize（默认 10，上限 100）。
# 响应：分页 {list,total,page,pageSize}，列表项为 admin_order_out（批量补商品图片，手机号脱敏）。
# 排序：created_at 倒序，同一时间再按 id 倒序。
@router.get("/orders", summary="订单列表（多条件筛选 + 快捷筛选 + 分页）")
async def list_orders(
    keyword: str | None = None,
    userKeyword: str | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
    payMethod: str | None = None,
    status: str | None = None,
    deliveryType: str | None = None,
    quick: str | None = None,
    page: int = 1,
    pageSize: int = 10,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    page, page_size, offset = _page_args(page, pageSize)
    stmt = _order_stmt(
        keyword, userKeyword, startDate, endDate, payMethod, status, deliveryType, quick
    )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await db.execute(
            stmt.order_by(Order.created_at.desc(), Order.id.desc()).offset(offset).limit(page_size)
        )
    ).all()
    goods_map = await _goods_map(db, [o for o, _ in rows])
    items = [admin_order_out(o, u, goods_map.get(o.goods_id)) for o, u in rows]
    return _paged(items, total, page, page_size)


# 接口：GET /admin/orders/{order_no} — 订单详情（需管理员 token，所有角色可读）
# 路径参数：order_no（订单编号，非自增 id）。
# 响应：admin_order_detail_out = 列表字段 + goodsDetail（商品明细）+ userInfo（用户摘要）+ logs（订单日志，按 id 升序）。
# 订单不存在抛 404；用户 / 商品已删除时对应摘要按空值降级展示。
@router.get("/orders/{order_no}", summary="订单详情（含商品明细 / 支付 / 物流 / 操作日志）")
async def order_detail(
    order_no: str,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    order = await db.scalar(select(Order).where(Order.order_no == order_no))
    if not order:
        raise _res_404("订单")
    user = await db.get(User, order.user_id)
    goods = await db.get(Goods, order.goods_id) if order.goods_id else None
    logs = (
        await db.scalars(
            select(OrderLog).where(OrderLog.order_no == order_no).order_by(OrderLog.id.asc())
        )
    ).all()
    return ok(admin_order_detail_out(order, user, goods, logs))


async def _get_order(db: AsyncSession, order_no: str) -> Order:
    """按订单编号取订单，不存在时抛 404；供各订单写接口复用。"""
    order = await db.scalar(select(Order).where(Order.order_no == order_no))
    if not order:
        raise _res_404("订单")
    return order


# 接口：POST /admin/orders/{order_no}/close — 关闭订单（需管理员 token + 订单写权限）
# 无请求体。状态机：仅 "待支付" 可关闭 → "已关闭"，并写 closed_at；其他状态返回业务码 1001。
# 响应：admin_order_out；只读角色调用抛 403。
@router.post("/orders/{order_no}/close", summary="关闭待支付订单")
async def close_order(
    order_no: str,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    if order.status != "待支付":
        return fail(1001, "仅待支付订单可以关闭")
    order.status = "已关闭"
    order.closed_at = datetime.now()
    _order_log(db, order_no, "close", "后台关闭订单（待支付 → 已关闭）", admin, request)
    await db.commit()
    return ok(admin_order_out(order, await db.get(User, order.user_id)))


# 接口：POST /admin/orders/{order_no}/ship — 订单发货（需管理员 token + 订单写权限）
# 入参：OrderShipIn{logisticsCompany, logisticsNo, deliveryType(可选)}；物流公司与单号必填、≤64。
# 状态机：仅 "待发货" 可发货 → "已发货"，写 logistics_company / logistics_no / shipped_at；
#        deliveryType 仅接受 "快递" / "自提"，其他值忽略（保留原配送方式）。
# 响应：admin_order_out；状态不符返回业务码 1001。
@router.post("/orders/{order_no}/ship", summary="待发货订单发货（录入物流单号）")
async def ship_order(
    order_no: str,
    payload: OrderShipIn,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    if order.status != "待发货":
        return fail(1001, "仅待发货订单可以发货")
    order.logistics_company = payload.logisticsCompany
    order.logistics_no = payload.logisticsNo
    if payload.deliveryType in ("快递", "自提"):
        order.delivery_type = payload.deliveryType
    order.status = "已发货"
    order.shipped_at = datetime.now()
    _order_log(
        db,
        order_no,
        "ship",
        f"后台发货：{payload.logisticsCompany} {payload.logisticsNo}（待发货 → 已发货）",
        admin,
        request,
    )
    await db.commit()
    return ok(admin_order_out(order, await db.get(User, order.user_id)))


# 接口：PUT /admin/orders/{order_no}/address — 修改收货地址（需管理员 token + 订单写权限）
# 入参：OrderAddressIn{receiver, receiverPhone, address}，三者必填、≤255。
# 状态机：仅 "待发货" / "已发货" 可修改；订单日志记录「旧值 → 新值」。
# 响应：admin_order_out；其他状态返回业务码 1001。
@router.put("/orders/{order_no}/address", summary="修改收货地址（待发货 / 已发货）")
async def update_order_address(
    order_no: str,
    payload: OrderAddressIn,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    if order.status not in ("待发货", "已发货"):
        return fail(1001, "仅待发货 / 已发货订单可以修改收货地址")
    old = f"{order.receiver or '--'} {order.receiver_phone or '--'} {order.address or '--'}"
    order.receiver = payload.receiver
    order.receiver_phone = payload.receiverPhone
    order.address = payload.address
    _order_log(
        db,
        order_no,
        "address",
        f"修改收货地址：{old} → {payload.receiver} {payload.receiverPhone} {payload.address}",
        admin,
        request,
    )
    await db.commit()
    return ok(admin_order_out(order, await db.get(User, order.user_id)))


# 接口：POST /admin/orders/{order_no}/remark — 订单备注 / 异常标记（需管理员 token + 订单写权限）
# 入参：OrderRemarkIn{remark(可选), abnormal(可选, bool)}；两者均为 None 时返回业务码 1001。
# 行为：remark 会 strip 后保存（空串表示清空，日志记「（已清空）」）；abnormal 真/假分别标记 / 取消异常；
#       任一字段有变更都会写订单日志。响应 admin_order_out。不限订单状态。
@router.post("/orders/{order_no}/remark", summary="订单备注 / 异常标记")
async def remark_order(
    order_no: str,
    payload: OrderRemarkIn,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    parts = []
    if payload.remark is not None:
        order.remark = payload.remark.strip()
        parts.append(f"备注：{order.remark or '（已清空）'}")
    if payload.abnormal is not None:
        order.abnormal = bool(payload.abnormal)
        parts.append("标记为异常订单" if order.abnormal else "取消异常标记")
    if not parts:
        return fail(1001, "没有需要保存的内容")
    _order_log(db, order_no, "remark", "；".join(parts), admin, request)
    await db.commit()
    return ok(admin_order_out(order, await db.get(User, order.user_id)))


# 接口：POST /admin/orders/{order_no}/refund — 处理退款申请（需管理员 token + 订单写权限）
# 入参：OrderRefundIn{amount(≥0), reason, approve(默认 true)}。
# 前置校验：已关闭 / 已退款不可退款；待支付无需退款（提示直接关闭）。
# 驳回（approve=false）：refund_status="rejected"；若当前状态为 "退款中"，回退到 "已发货"（有发货时间）或 "待发货"。
# 通过：amount 必须 > 0 且不超过实付金额；全额 → "已退款"，部分 → "部分退款"，refund_status="approved"；
#      全额时返还抵扣积分（add_points）并按 qty 恢复商品库存，保证积分与库存账目一致。
# 响应：admin_order_out。
@router.post("/orders/{order_no}/refund", summary="退款 / 部分退款申请处理")
async def refund_order(
    order_no: str,
    payload: OrderRefundIn,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """处理退款申请：approve=true 通过（全额 → 已退款，部分 → 部分退款）；false 驳回。"""
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    if order.status in ("已关闭", "已退款"):
        return fail(1001, "该订单当前状态不支持退款")
    if order.status == "待支付":
        return fail(1001, "未支付订单无需退款，请直接关闭")

    amount = round(float(payload.amount or 0), 2)
    reason = (payload.reason or "").strip()

    if not payload.approve:
        order.refund_status = "rejected"
        order.refund_reason = reason
        # 驳回后把「退款中」还原为发货前状态，避免订单卡在退款中
        if order.status == "退款中":
            order.status = "已发货" if order.shipped_at else "待发货"
        _order_log(db, order_no, "refund", f"驳回退款申请：{reason or '未填写原因'}", admin, request)
        await db.commit()
        return ok(admin_order_out(order, await db.get(User, order.user_id)))

    if amount <= 0:
        return fail(1001, "请输入退款金额")
    if amount > round(order.pay_amount or 0, 2):
        return fail(1001, "退款金额不能超过实付金额")

    is_full = amount >= round(order.pay_amount or 0, 2)
    order.refund_amount = amount
    order.refund_status = "approved"
    order.refund_reason = reason
    order.status = "已退款" if is_full else "部分退款"

    user = await db.get(User, order.user_id)
    if is_full:
        # 全额退款：退回抵扣积分、恢复库存，保证账目一致
        if order.use_points and user:
            await add_points(db, user, order.use_points, f"订单 {order_no} 退款返还")
        if order.goods_id:
            goods = await db.get(Goods, order.goods_id)
            if goods:
                goods.stock = (goods.stock or 0) + (order.qty or 0)

    _order_log(
        db,
        order_no,
        "refund",
        f"{'全额' if is_full else '部分'}退款 ¥{amount}：{reason or '未填写原因'}",
        admin,
        request,
    )
    await db.commit()
    return ok(admin_order_out(order, user))


# 接口：POST /admin/orders/{order_no}/complete — 确认完成订单（需管理员 token + 订单写权限）
# 无请求体。状态机：仅 "已发货" → "已完成"，并写 completed_at；其他状态返回业务码 1001。
# 响应：admin_order_out。
@router.post("/orders/{order_no}/complete", summary="确认完成订单（已发货 → 已完成）")
async def complete_order(
    order_no: str,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    if order.status != "已发货":
        return fail(1001, "仅已发货订单可以完成")
    order.status = "已完成"
    order.completed_at = datetime.now()
    _order_log(db, order_no, "complete", "确认收货，订单完成（已发货 → 已完成）", admin, request)
    await db.commit()
    return ok(admin_order_out(order, await db.get(User, order.user_id)))


# 接口：DELETE /admin/orders/{order_no} — 删除订单（需管理员 token + 订单写权限）
# 行为：物理删除订单，并清理该订单的全部 OrderLog，同时在全局操作日志留痕；
#       删除不会自动退款 / 恢复库存，需先走退款流程。响应 data 为空；订单不存在抛 404。
@router.delete("/orders/{order_no}", summary="删除订单（同步清理订单日志，记录操作日志）")
async def delete_order(
    order_no: str,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除订单：不可恢复，且删除不会自动退款（需先走退款流程）。

    同步清理该订单的订单操作日志，并在全局操作日志中留痕。
    """
    _require_order_write(admin)
    order = await _get_order(db, order_no)
    detail = (
        f"删除订单 {order.order_no}（{order.status}）："
        f"{order.goods_name} × {order.qty}，实付 ¥{order.pay_amount}"
    )
    await db.delete(order)
    logs = (await db.scalars(select(OrderLog).where(OrderLog.order_no == order_no))).all()
    for log in logs:
        await db.delete(log)
    oplog.add_log(
        db,
        action="order.delete",
        admin=admin,
        target=order_no,
        detail=detail,
        ip=_admin_ip(request),
    )
    await db.commit()
    return ok()