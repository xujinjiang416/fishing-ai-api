"""user.py — 用户资料 / 位置 / 收藏 / 积分 / 订单 / 我的评论 / 会员

说明：随「移除广告解锁」需求，本模块已删除 `/user/unlock` 与 `/user/unlocks`
（功能单次解锁：看广告 / 付费解锁本次）两个接口及其依赖的 `FeatureUnlock` 模型；
会员开通改为直接购买后台配置的套餐。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.deps import get_current_user_id, get_db
from ...core.response import fail, ok
from ...core.utils import add_days, gen_no, today
from ...models import (
    Favorite,
    MembershipOrder,
    Order,
    Plan,
    PointsLog,
    Spot,
    SpotComment,
    User,
)
from ...schemas.schemas import (
    FavoriteIn,
    LocationIn,
    MembershipIn,
    ProfileIn,
    membership_order_out,
    order_out,
    spot_out,
    user_out,
)

router = APIRouter(prefix="/user", tags=["用户"])


async def _user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


async def _roll_ad_daily(db: AsyncSession, user: User) -> None:
    """跨天重置广告计数"""
    if user.ads_date != today():
        user.ads_date = today()
        user.ads_watched_today = 0
        user.ad_points_today = 0
        await db.commit()


async def add_points(db: AsyncSession, user: User, change: int, reason: str) -> int:
    """统一的积分变动入口（写流水，保证可对账）"""
    user.points = max(0, user.points + change)
    if change > 0:
        user.points_total += change
    db.add(PointsLog(user_id=user.id, change=change, reason=reason, balance=user.points))
    return user.points


@router.get("/profile", summary="获取我的资料")
async def profile(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    user = await _user(db, user_id)
    await _roll_ad_daily(db, user)
    return ok(user_out(user))


@router.put("/profile", summary="修改我的资料")
async def update_profile(
    payload: ProfileIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user = await _user(db, user_id)
    if payload.nickname:
        user.nickname = payload.nickname
        user.avatar_text = payload.nickname[:1]
    if payload.city:
        user.city = payload.city
    if payload.phone:
        user.phone = payload.phone
    await db.commit()
    return ok(user_out(user))


@router.post("/location", summary="设置定位（省 / 市 / 区）")
async def set_location(
    payload: LocationIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    user = await _user(db, user_id)
    user.location = {
        "province": payload.province,
        "city": payload.city,
        "district": payload.district,
        "source": "manual",
    }
    await db.commit()
    return ok(user.location)


@router.get("/favorites", summary="我的收藏野塘")
async def favorites(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    rows = await db.scalars(select(Favorite).where(Favorite.user_id == user_id))
    ids = [f.spot_id for f in rows.all()]
    spots = []
    if ids:
        spots = list(await db.scalars(select(Spot).where(Spot.id.in_(ids))))
    return ok({"ids": ids, "spots": [spot_out(s) for s in spots]})


@router.post("/favorites", summary="收藏 / 取消收藏（幂等切换）")
async def toggle_favorite(
    payload: FavoriteIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    row = await db.scalar(
        select(Favorite).where(Favorite.user_id == user_id, Favorite.spot_id == payload.spotId)
    )
    if row:
        await db.delete(row)
        await db.commit()
        return ok({"spotId": payload.spotId, "favorited": False})

    db.add(Favorite(user_id=user_id, spot_id=payload.spotId))
    await db.commit()
    return ok({"spotId": payload.spotId, "favorited": True})


@router.get("/points/logs", summary="积分流水")
async def points_logs(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    rows = await db.scalars(
        select(PointsLog)
        .where(PointsLog.user_id == user_id)
        .order_by(PointsLog.id.desc())
        .limit(50)
    )
    user = await _user(db, user_id)
    return ok(
        {
            "points": user.points,
            "rate": settings.points_per_yuan,
            "logs": [
                {
                    "change": r.change,
                    "reason": r.reason,
                    "balance": r.balance,
                    "date": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                }
                for r in rows.all()
            ],
        }
    )


@router.get("/orders", summary="我的订单")
async def my_orders(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    rows = await db.scalars(
        select(Order).where(Order.user_id == user_id).order_by(Order.id.desc())
    )
    return ok([order_out(o) for o in rows.all()])


@router.get("/comments", summary="我的评论")
async def my_comments(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(SpotComment, Spot.name)
        .join(Spot, Spot.id == SpotComment.spot_id)
        .where(SpotComment.user_id == user_id)
        .order_by(SpotComment.id.desc())
    )
    return ok(
        [
            {
                "id": c.id,
                "spot": spot_name,
                "text": c.text,
                "date": c.created_at.strftime("%Y-%m-%d") if c.created_at else "",
                "images": c.images or [],
            }
            for c, spot_name in result.all()
        ]
    )


@router.get("/membership", summary="会员状态与套餐（套餐来自后台配置）")
async def membership(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """返回当前会员状态 + 所有「已上架」的会员套餐（供开通页 / 解锁弹窗展示）。"""
    user = await _user(db, user_id)
    rows = (
        await db.scalars(
            select(Plan)
            .where((Plan.status.is_(None)) | (Plan.status == "on"))
            .order_by(Plan.sort, Plan.id)
        )
    ).all()
    return ok(
        {
            "isMember": bool(user.is_member),
            "memberExpire": user.member_expire,
            "plan": user.plan,
            "planName": user.plan_name,
            "plans": [
                {
                    "id": p.id,
                    "name": p.name,
                    "duration": p.duration,
                    "durationLabel": p.duration_label,
                    "price": p.price,
                    "origPrice": p.orig_price,
                    "icon": p.icon,
                    "tag": p.tag,
                    "tagType": p.tag_type,
                    "benefits": p.benefits or [],
                }
                for p in rows
            ],
        }
    )


@router.post("/membership", summary="开通 / 续费会员（按后台套餐）")
async def buy_membership(
    payload: MembershipIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """按所选套餐开通/续费：金额与时长取自后台套餐配置，并同步会员状态。"""
    user = await _user(db, user_id)
    plan_id = (payload.plan or "").strip()
    plan = await db.get(Plan, plan_id) if plan_id else None
    if not plan:
        return fail(1001, "套餐不存在，请重新选择")
    if (plan.status or "on") == "off":
        return fail(1001, "该套餐已下架，请选择其他套餐")

    days = plan.duration or 30
    months = max(1, round(days / 30))

    order = MembershipOrder(
        order_no=gen_no("M"),
        user_id=user.id,
        plan=plan.id,
        months=months,
        amount=plan.price,
        status="已支付",
    )
    db.add(order)

    # 同步会员状态：小程序端 / 管理后台看到的是同一份 users 数据
    user.is_member = True
    user.member_expire = add_days(user.member_expire, days)
    user.plan = plan.id
    user.plan_name = plan.name
    user.plan_expire = user.member_expire
    # 累计销量，用于后台「会员套餐购买数据统计」
    plan.sales = (plan.sales or 0) + 1

    await db.commit()
    await db.refresh(order)
    data = membership_order_out(order)
    data["memberExpire"] = user.member_expire
    data["planName"] = plan.name
    data["days"] = days
    return ok(data)
