"""shop.py — 商城商品 / 下单"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.deps import get_current_user_id, get_db
from ...core.response import fail, ok
from ...core.utils import gen_no
from ...models import Goods, Order, User
from ...schemas.schemas import OrderIn, goods_out, order_out
from ...services import wechat
from .user import add_points

router = APIRouter(tags=["商城"])


@router.get("/goods", summary="商品列表（可按分类筛选，只显示上架商品）")
async def list_goods(cat: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Goods).where(or_(Goods.status.is_(None), Goods.status != "off"))
    if cat and cat != "全部":
        stmt = stmt.where(Goods.cat == cat)
    rows = await db.scalars(stmt)
    return ok([goods_out(g) for g in rows.all()])


@router.get("/goods/{goods_id}", summary="商品详情")
async def goods_detail(goods_id: str, db: AsyncSession = Depends(get_db)):
    goods = await db.get(Goods, goods_id)
    if not goods:
        raise HTTPException(status_code=404, detail="商品不存在")
    return ok(goods_out(goods))


@router.post("/orders", summary="下单（支持积分抵扣）")
async def create_order(
    payload: OrderIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    goods = await db.get(Goods, payload.goodsId)
    if not goods:
        raise HTTPException(status_code=404, detail="商品不存在")
    if goods.stock < payload.qty:
        return fail(1001, "库存不足")

    user = await db.get(User, user_id)
    total = round(goods.price * payload.qty, 2)

    # 积分抵扣上限：不超过订单金额，也不超过用户可用积分
    max_points = min(user.points, int(total * settings.points_per_yuan))
    use_points = min(payload.usePoints, max_points)
    pay_amount = max(0.01, round(total - use_points / settings.points_per_yuan, 2))

    if use_points:
        await add_points(db, user, -use_points, "订单抵扣")
    goods.stock -= payload.qty

    order = Order(
        order_no=gen_no("F"),
        user_id=user.id,
        goods_id=goods.id,
        goods_name=goods.name,
        qty=payload.qty,
        total=total,
        use_points=use_points,
        pay_amount=pay_amount,
        status="待发货",
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)

    payment = wechat.create_payment(order.order_no, pay_amount, user.openid)
    data = order_out(order)
    data.update(
        {"orderNo": order.order_no, "payAmount": pay_amount, "usePoints": use_points, "payment": payment}
    )
    return ok(data)
