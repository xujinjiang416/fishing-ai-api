"""shop.py — 商城商品 / 下单

职责：小程序端商品浏览与下单，支持积分抵扣，下单后调用微信支付统一下单。

鉴权要求：商品列表 / 详情为公开接口；下单必须登录（Authorization: Bearer <user token>）。

依赖服务：MySQL（goods / orders / order_logs / user_addresses / users 表）、
services.wechat.create_payment（支付参数，未配置 appid 时返回模拟支付结果）、
user.add_points（积分扣减并写流水）。

注意事项：下单会快照收货地址与商品信息，地址后续修改不影响历史订单；
积分抵扣受「不超过订单金额」与「不超过用户可用积分」双重上限约束。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.deps import get_current_user_id, get_db
from ...core.response import fail, ok
from ...core.utils import gen_no
from ...models import Goods, Order, OrderLog, User, UserAddress
from ...schemas.schemas import OrderIn, goods_out, order_out
from ...services import wechat
from .user import add_points

router = APIRouter(tags=["商城"])


@router.get("/goods", summary="商品列表（可按分类筛选，只显示上架商品）")
async def list_goods(cat: str | None = None, db: AsyncSession = Depends(get_db)):
    """商品列表。

    方法与路径：GET /goods（公开）。
    查询参数：cat 分类名，为空或「全部」时不筛选。
    响应：商品数组（goods_out），仅返回上架商品（status 为空或非 "off"）。
    说明：不分页，按数据库默认顺序返回。
    """
    stmt = select(Goods).where(or_(Goods.status.is_(None), Goods.status != "off"))
    if cat and cat != "全部":
        stmt = stmt.where(Goods.cat == cat)
    rows = await db.scalars(stmt)
    return ok([goods_out(g) for g in rows.all()])


@router.get("/goods/{goods_id}", summary="商品详情")
async def goods_detail(goods_id: str, db: AsyncSession = Depends(get_db)):
    """商品详情。

    方法与路径：GET /goods/{goods_id}（公开）。
    响应：单个商品结构；商品不存在返回 404（不区分是否已下架）。
    """
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
    """下单（支持积分抵扣）。

    方法与路径：POST /orders（需登录）。
    请求参数 OrderIn：goodsId 商品 id；qty 数量（≥1，默认 1）；usePoints 拟用积分（≥0，默认 0）；
    addressId 收货地址 id（必填且必须属于当前用户）；payMethod / deliveryType 可选。
    校验：商品存在（否则 404）；库存充足（否则业务错误 1001）；地址归属校验失败返回 1002。
    金额：total = 单价 × 数量；可抵扣积分上限 = min(用户积分, total × points_per_yuan)；
    实付金额 = max(0.01, total - 抵扣积分 / points_per_yuan)，最低 0.01 元。
    副作用（同一次 commit）：扣减积分并写流水、扣减库存、创建订单与 create 日志；
    收货信息按所选地址快照（省 + 市（与省不同时）+ 区 + 详细地址）。
    响应：订单结构 + orderNo / payAmount / usePoints / payment（微信支付参数或模拟结果）。
    """
    goods = await db.get(Goods, payload.goodsId)
    if not goods:
        raise HTTPException(status_code=404, detail="商品不存在")
    if goods.stock < payload.qty:
        return fail(1001, "库存不足")

    # 支付前强制校验：必须选择本人名下的收货地址
    address = None
    if payload.addressId:
        address = await db.scalar(
            select(UserAddress).where(
                UserAddress.id == payload.addressId, UserAddress.user_id == user_id
            )
        )
    if not address:
        return fail(1002, "请先填写收货地址后再支付")

    user = await db.get(User, user_id)
    total = round(goods.price * payload.qty, 2)

    # 积分抵扣上限：不超过订单金额，也不超过用户可用积分
    max_points = min(user.points, int(total * settings.points_per_yuan))
    use_points = min(payload.usePoints, max_points)
    pay_amount = max(0.01, round(total - use_points / settings.points_per_yuan, 2))

    if use_points:
        await add_points(db, user, -use_points, "订单抵扣")
    goods.stock -= payload.qty

    # 收货信息取所选地址快照（避免地址后续修改影响历史订单）
    region = address.province + (address.city if address.city and address.city != address.province else "") + address.district
    full_address = region + address.detail

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
        pay_method=payload.payMethod or "微信支付",
        delivery_type=payload.deliveryType or "快递",
        receiver=address.receiver,
        receiver_phone=address.phone,
        address=full_address,
        paid_at=datetime.now(),
    )
    db.add(order)
    # 订单操作日志：下单记录，供后台订单详情全链路追溯
    db.add(
        OrderLog(
            order_no=order.order_no,
            action="create",
            operator=user.nickname or "用户",
            detail=f"用户下单：{goods.name} × {payload.qty}，实付 ¥{pay_amount}",
        )
    )
    await db.commit()
    await db.refresh(order)

    payment = wechat.create_payment(order.order_no, pay_amount, user.openid)
    data = order_out(order)
    data.update(
        {"orderNo": order.order_no, "payAmount": pay_amount, "usePoints": use_points, "payment": payment}
    )
    return ok(data)
