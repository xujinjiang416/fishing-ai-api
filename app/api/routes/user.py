"""user.py — 用户资料 / 位置 / 收藏 / 积分 / 订单 / 我的评论 / 会员

职责：小程序端「我的」全量接口：个人资料、定位、收货地址、收藏、积分流水、
订单与物流、我的评论、会员状态与开通。

鉴权要求：本文件所有接口均需用户登录（Authorization: Bearer <user token>）；
地址 / 订单 / 评论均按当前用户做归属校验，禁止越权访问他人数据。

依赖服务：MySQL（users / user_addresses / favorites / points_logs / orders / goods /
spot_comments / plans / membership_orders / 行政区划表）、core.config（points_per_yuan）、
core.deps（JWT 解析后注入 user_id）。

注意事项：
- add_points 是全局统一积分变动入口（rewards / shop 亦复用），必须写流水以便对账；
- 积分扣减不会为负（下限 0），累计获得（points_total）只统计正向变动。

说明：随「移除广告解锁」需求，本模块已删除 `/user/unlock` 与 `/user/unlocks`
（功能单次解锁：看广告 / 付费解锁本次）两个接口及其依赖的 `FeatureUnlock` 模型；
会员开通改为直接购买后台配置的套餐。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.deps import get_current_user_id, get_db
from ...core.response import fail, ok
from ...core.utils import add_days, gen_no, today
from ...models import (
    City,
    District,
    Favorite,
    Goods,
    MembershipOrder,
    Order,
    Plan,
    PointsLog,
    Province,
    Spot,
    SpotComment,
    User,
    UserAddress,
)
from ...schemas.schemas import (
    AddressIn,
    FavoriteIn,
    LocationIn,
    MembershipIn,
    ProfileIn,
    address_out,
    membership_order_out,
    spot_out,
    user_order_out,
    user_out,
)

router = APIRouter(prefix="/user", tags=["用户"])


async def _user(db: AsyncSession, user_id: int) -> User:
    """按主键取用户；不存在时返回 401（token 对应用户已被删除）。"""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


async def _roll_ad_daily(db: AsyncSession, user: User) -> None:
    """跨天重置广告计数（惰性重置：读取资料时顺带执行）。

    用户当天首次访问时把 ads_watched_today / ad_points_today 归零；
    同日重复调用不会产生写入。
    """
    if user.ads_date != today():
        user.ads_date = today()
        user.ads_watched_today = 0
        user.ad_points_today = 0
        await db.commit()


async def add_points(db: AsyncSession, user: User, change: int, reason: str) -> int:
    """统一的积分变动入口（写流水，保证可对账）。

    change 为正表示获得（同时累加 points_total），为负表示扣减；
    余额下限为 0，扣减不会把 points 打成负数。调用方负责 commit。
    """
    user.points = max(0, user.points + change)
    if change > 0:
        user.points_total += change
    db.add(PointsLog(user_id=user.id, change=change, reason=reason, balance=user.points))
    return user.points


@router.get("/profile", summary="获取我的资料")
async def profile(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """获取我的资料。

    方法与路径：GET /user/profile（需登录）。
    响应：用户对外结构（含用户名，不含手机号，仅返回 phoneBound 绑定状态）；
    顺带执行广告计数跨天重置。
    """
    user = await _user(db, user_id)
    await _roll_ad_daily(db, user)
    return ok(user_out(user))


@router.put("/profile", summary="修改我的资料")
async def update_profile(
    payload: ProfileIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """修改我的资料。

    方法与路径：PUT /user/profile（需登录）。
    请求参数 ProfileIn：nickname / city / phone 均可选，仅传入非空字段会被更新；
    phone 为后续手机号绑定预留入口，写入 users.phone 但响应不回传手机号；
    nickname 变更时同步头像文字（取首字）。
    响应：更新后的用户结构。
    """
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
    """设置定位（省 / 市 / 区）。

    方法与路径：POST /user/location（需登录）。
    请求参数 LocationIn：province / city / district 必填。
    行为：整体覆盖 users.location，并标记 source=manual（区别于自动定位）。
    响应：保存后的定位对象。
    """
    user = await _user(db, user_id)
    user.location = {
        "province": payload.province,
        "city": payload.city,
        "district": payload.district,
        "source": "manual",
    }
    await db.commit()
    return ok(user.location)


@router.get("/addresses", summary="收货地址列表（默认地址置顶）")
async def list_addresses(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """收货地址列表。

    方法与路径：GET /user/addresses（需登录）。
    响应：address_out 数组，默认地址置顶，其余按 id 倒序。
    """
    rows = await db.scalars(
        select(UserAddress)
        .where(UserAddress.user_id == user_id)
        .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
    )
    return ok([address_out(a) for a in rows.all()])


@router.get("/addresses/{address_id}", summary="收货地址详情")
async def get_address(
    address_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """收货地址详情。

    方法与路径：GET /user/addresses/{address_id}（需登录）。
    越权校验：仅能读取本人地址，非本人或不存在统一返回 404。
    """
    return ok(address_out(await _own_address(db, user_id, address_id)))


@router.post("/addresses", summary="新增收货地址")
async def create_address(
    payload: AddressIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """新增收货地址。

    方法与路径：POST /user/addresses（需登录）。
    请求参数 AddressIn：receiver / phone / province / city / district / detail 必填
    （去空白，≤255 字）；isDefault 表示是否设为默认。
    限制：单用户最多 20 条（超出返回 1001）；省 / 市 / 区需在行政区划表中存在
    （行政区划表为空时跳过校验）；首条地址自动设为默认，设默认前先清除其他默认标记。
    响应：新建地址结构。
    """
    count = await db.scalar(
        select(func.count()).select_from(UserAddress).where(UserAddress.user_id == user_id)
    ) or 0
    if count >= 20:
        return fail(1001, "最多保存 20 条收货地址")
    if not await _valid_region(db, payload.province, payload.city, payload.district):
        return fail(1001, "省 / 市 / 区信息不合法，请重新选择")

    # 首条地址自动设为默认
    is_default = payload.isDefault or count == 0
    if is_default:
        await _clear_default(db, user_id)

    address = UserAddress(
        user_id=user_id,
        receiver=payload.receiver,
        phone=payload.phone,
        province=payload.province,
        city=payload.city,
        district=payload.district,
        detail=payload.detail,
        is_default=is_default,
    )
    db.add(address)
    await db.commit()
    await db.refresh(address)
    return ok(address_out(address))


@router.put("/addresses/{address_id}", summary="编辑收货地址")
async def update_address(
    address_id: int,
    payload: AddressIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """编辑收货地址。

    方法与路径：PUT /user/addresses/{address_id}（需登录）。
    越权校验：仅能编辑本人地址，非本人或不存在返回 404。
    校验：省 / 市 / 区需合法；isDefault=true 时先清除其他默认标记，
    isDefault=false 时该地址取消默认。
    响应：更新后的地址结构。
    """
    address = await _own_address(db, user_id, address_id)
    if not await _valid_region(db, payload.province, payload.city, payload.district):
        return fail(1001, "省 / 市 / 区信息不合法，请重新选择")
    if payload.isDefault:
        await _clear_default(db, user_id)

    address.receiver = payload.receiver
    address.phone = payload.phone
    address.province = payload.province
    address.city = payload.city
    address.district = payload.district
    address.detail = payload.detail
    address.is_default = bool(payload.isDefault)

    await db.commit()
    await db.refresh(address)
    return ok(address_out(address))


@router.post("/addresses/{address_id}/default", summary="设为默认地址")
async def set_default_address(
    address_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """设为默认地址。

    方法与路径：POST /user/addresses/{address_id}/default（需登录）。
    行为：先清除该用户其他默认标记，再置当前地址为默认；仅能操作本人地址。
    响应：更新后的地址结构。
    """
    address = await _own_address(db, user_id, address_id)
    await _clear_default(db, user_id)
    address.is_default = True
    await db.commit()
    await db.refresh(address)
    return ok(address_out(address))


@router.delete("/addresses/{address_id}", summary="删除收货地址（默认地址删除后自动顺延）")
async def delete_address(
    address_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """删除收货地址。

    方法与路径：DELETE /user/addresses/{address_id}（需登录）。
    行为：删除本人地址；若删除的是默认地址，自动把剩余最早创建的一条设为默认。
    响应：{ deleted: 被删地址 id, defaultId: 新的默认地址 id（无则为 null） }。
    """
    address = await _own_address(db, user_id, address_id)
    was_default = bool(address.is_default)
    await db.delete(address)
    await db.flush()

    new_default_id = None
    if was_default:
        # 删除的是默认地址：自动把剩余地址中的第一条设为默认
        first = await db.scalar(
            select(UserAddress)
            .where(UserAddress.user_id == user_id)
            .order_by(UserAddress.id.asc())
            .limit(1)
        )
        if first:
            first.is_default = True
            new_default_id = first.id
    await db.commit()
    return ok({"deleted": address_id, "defaultId": new_default_id})


async def _own_address(db: AsyncSession, user_id: int, address_id: int) -> UserAddress:
    """取当前用户名下的地址；不存在或不属于本人统一返回 404（防止越权探测）。"""
    address = await db.scalar(
        select(UserAddress).where(
            UserAddress.id == address_id, UserAddress.user_id == user_id
        )
    )
    if not address:
        raise HTTPException(status_code=404, detail="收货地址不存在")
    return address


async def _clear_default(db: AsyncSession, user_id: int) -> None:
    """清除该用户全部默认地址标记（批量 UPDATE，不提交事务，由调用方 commit）。"""
    await db.execute(
        update(UserAddress)
        .where(UserAddress.user_id == user_id, UserAddress.is_default.is_(True))
        .values(is_default=False)
    )


async def _valid_region(db: AsyncSession, province: str, city: str, district: str) -> bool:
    """校验省 / 市 / 区三级行政区划是否存在。

    政务/测试库未导入行政区划数据时跳过校验（避免阻断主流程）。
    返回 True 表示校验通过（含跳过场景），False 表示省 / 市 / 区不合法。
    """
    total = await db.scalar(select(func.count()).select_from(Province)) or 0
    if total == 0:
        return True
    p = await db.scalar(select(Province).where(Province.name == province))
    if not p:
        return False
    c = await db.scalar(select(City).where(City.province_id == p.id, City.name == city))
    if not c:
        return False
    d = await db.scalar(select(District).where(District.city_id == c.id, District.name == district))
    return d is not None


@router.get("/favorites", summary="我的收藏野塘")
async def favorites(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """我的收藏野塘。

    方法与路径：GET /user/favorites（需登录）。
    响应：{ ids: 收藏的钓点 id 列表, spots: 对应钓点结构数组 }；
    收藏的钓点即使已下架也一并返回。
    """
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
    """收藏 / 取消收藏（幂等切换）。

    方法与路径：POST /user/favorites（需登录）。
    请求参数 FavoriteIn：spotId 钓点 id。
    行为：已收藏则删除记录并返回 favorited=false，未收藏则新增并返回 favorited=true；
    重复调用同一状态不会产生重复数据。不校验钓点是否存在。
    """
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
    """积分流水。

    方法与路径：GET /user/points/logs（需登录）。
    响应：{ points: 当前余额, rate: 积分兑元比例, logs: 最近 50 条流水 }，
    流水按 id 倒序，含变动值 / 原因 / 变动后余额 / 时间。
    """
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
    """我的订单列表。

    方法与路径：GET /user/orders（需登录）。
    响应：订单数组（含商品图片 / 分类 / 规格摘要），按 id 倒序；
    商品信息批量查询，避免 N+1。
    """
    rows = await db.scalars(
        select(Order).where(Order.user_id == user_id).order_by(Order.id.desc())
    )
    orders = list(rows.all())
    # 批量取商品信息（图片 / 分类 / 规格），避免 N+1
    goods_ids = {o.goods_id for o in orders if o.goods_id}
    goods_map: dict[str, Goods] = {}
    if goods_ids:
        goods_rows = (await db.scalars(select(Goods).where(Goods.id.in_(goods_ids)))).all()
        goods_map = {g.id: g for g in goods_rows}
    return ok([user_order_out(o, goods_map.get(o.goods_id)) for o in orders])


@router.get("/orders/{order_no}", summary="订单详情（小程序端）")
async def my_order_detail(
    order_no: str,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """订单详情（小程序端）。

    仅允许查看自己的订单；返回商品、金额、收货与物流摘要。
    方法与路径：GET /user/orders/{order_no}（需登录）。
    越权校验：查询条件同时限定 order_no 与 user_id，非本人订单返回 404。
    """
    order = await db.scalar(
        select(Order).where(Order.order_no == order_no, Order.user_id == user_id)
    )
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    goods = await db.get(Goods, order.goods_id) if order.goods_id else None
    return ok(user_order_out(order, goods))


@router.get("/orders/{order_no}/logistics", summary="订单物流信息（公司 / 单号 / 官方电话）")
async def my_order_logistics(
    order_no: str,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """订单物流信息。

    物流信息刷新接口：只返回物流公司、运单号与官方查询电话，不含轨迹节点。
    方法与路径：GET /user/orders/{order_no}/logistics（需登录）。
    越权校验：仅本人订单可查，否则 404。
    响应：company / no / phone / deliveryType / shippedAt。
    """
    order = await db.scalar(
        select(Order).where(Order.order_no == order_no, Order.user_id == user_id)
    )
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    data = user_order_out(order)
    return ok(
        {
            "company": data["logisticsCompany"],
            "no": data["logisticsNo"],
            "phone": data["logisticsPhone"],
            "deliveryType": data["deliveryType"],
            "shippedAt": data["shippedAt"],
        }
    )


@router.get("/comments", summary="我的评论")
async def my_comments(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """我的评论。

    方法与路径：GET /user/comments（需登录）。
    响应：评论数组（含所属钓点名），按 id 倒序；日期取创建时间的前 10 位。
    """
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
    """会员状态与套餐。

    返回当前会员状态 + 所有「已上架」的会员套餐（供开通页 / 解锁弹窗展示）。
    方法与路径：GET /user/membership（需登录）。
    响应：isMember / memberExpire / plan / planName，以及 plans（status 为空或 on，
    按排序权重降序、创建时间降序）。
    """
    user = await _user(db, user_id)
    rows = (
        await db.scalars(
            select(Plan)
            .where((Plan.status.is_(None)) | (Plan.status == "on"))
            .order_by(Plan.sort.desc(), Plan.created_at.desc(), Plan.id)
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
    """开通 / 续费会员。

    按所选套餐开通 / 续费：金额与时长取自后台套餐配置，并同步会员状态。
    方法与路径：POST /user/membership（需登录）。
    请求参数 MembershipIn：plan 套餐 id。
    校验：套餐必须存在且未下架（否则业务错误 1001）。
    行为：创建「已支付」会员订单（订单号前缀 M）；member_expire 在原到期日基础上顺延
    套餐天数（duration，缺省 30 天），同步 is_member / plan / plan_name，并累加套餐销量。
    响应：会员订单结构 + memberExpire / planName / days。
    说明：当前为演示直通实现，未接入真实支付回调。
    """
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
