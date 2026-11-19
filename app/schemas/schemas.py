"""schemas.py — 请求体校验 + 响应序列化

约定：入参与出参一律使用前端既有的 camelCase 字段名，
避免前端再做一层字段映射；数据库内部仍是 snake_case。
"""

import re

from pydantic import BaseModel, Field, field_validator

from ..services.logistics import phone_of

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _check_phone(v: str) -> str:
    v = (v or "").strip().replace(" ", "").replace("-", "")
    if v.startswith("+86"):
        v = v[3:]
    if not PHONE_RE.match(v):
        raise ValueError("请输入正确的 11 位手机号")
    return v


def _check_password(v: str) -> str:
    if not isinstance(v, str) or not (6 <= len(v) <= 16):
        raise ValueError("密码需为 6-16 位")
    return v


# ---------------- 请求体 ----------------
class WechatLoginIn(BaseModel):
    code: str
    nickname: str | None = None


class PasswordLoginIn(BaseModel):
    """账号密码登录：account 必须是手机号，password 为 6-16 位。"""

    account: str
    password: str

    @field_validator("account")
    @classmethod
    def _v_account(cls, v: str) -> str:
        return _check_phone(v)

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        return _check_password(v)


class RegisterIn(BaseModel):
    """注册：手机号 + 密码（6-16 位），confirmPassword 传了就校验一致。"""

    phone: str
    password: str
    confirmPassword: str | None = None
    nickname: str | None = None

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str) -> str:
        return _check_phone(v)

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        return _check_password(v)

    @field_validator("confirmPassword")
    @classmethod
    def _v_confirm(cls, v: str | None, info) -> str | None:
        if v and v != info.data.get("password"):
            raise ValueError("两次输入的密码不一致")
        return v


class ProfileIn(BaseModel):
    nickname: str | None = None
    city: str | None = None
    phone: str | None = None


class LocationIn(BaseModel):
    province: str
    city: str
    district: str


class AddressIn(BaseModel):
    """收货地址新增 / 编辑入参。"""

    receiver: str
    phone: str
    province: str
    city: str
    district: str
    detail: str
    isDefault: bool = False

    @field_validator("receiver", "province", "city", "district", "detail")
    @classmethod
    def _v_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("收货信息不能为空")
        return v[:255]

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str) -> str:
        return _check_phone(v)


class CommentIn(BaseModel):
    text: str = ""
    rating: int = Field(default=5, ge=1, le=5)
    images: list[str] = []


class OrderIn(BaseModel):
    goodsId: str
    qty: int = Field(default=1, ge=1)
    usePoints: int = Field(default=0, ge=0)
    # 支付前必须选择收货地址（由后端校验并快照到订单）
    addressId: int | None = None
    # 以下为可选收货 / 配送信息（保留兼容，实际以下单时选择的地址为准）
    receiver: str | None = None
    receiverPhone: str | None = None
    address: str | None = None
    deliveryType: str | None = None
    payMethod: str | None = None


class MembershipIn(BaseModel):
    """开通会员：plan 为所选套餐 id（来自后台配置的套餐列表）。"""

    plan: str = ""


class FavoriteIn(BaseModel):
    spotId: str


# ---------------- 响应序列化 ----------------
def _checked_today(user) -> bool:
    from ..core.utils import today

    return user.last_checkin_date == today()


def mask_phone(phone: str | None) -> str | None:
    """手机号脱敏：13812345678 -> 138****5678"""
    p = (phone or "").strip()
    if len(p) < 7:
        return p or None
    return p[:3] + "****" + p[-4:]


def user_out(user) -> dict:
    return {
        "id": user.id,
        "uid": user.uid,
        "nickname": user.nickname,
        "avatarText": user.avatar_text,
        "phone": mask_phone(user.phone),
        "level": user.level,
        "joined": user.joined,
        "city": user.city,
        "isMember": bool(user.is_member),
        "memberExpire": user.member_expire,
        "points": user.points,
        "pointsTotal": user.points_total,
        "streak": user.streak,
        "maxStreak": user.max_streak,
        "lastCheckinDate": user.last_checkin_date,
        "checkedToday": _checked_today(user),
        "adsWatchedToday": user.ads_watched_today,
        "adPointsToday": user.ad_points_today,
        "location": user.location,
    }


def comment_out(c) -> dict:
    return {
        "id": c.id,
        "user": c.user_name,
        "avatar": c.avatar,
        "rating": c.rating,
        "text": c.text,
        "date": c.date,
        "images": c.images or [],
    }


def spot_out(spot, with_comments: bool = False) -> dict:
    data = {
        "id": spot.id,
        "name": spot.name,
        "dist": spot.dist,
        "walk": spot.walk,
        "rating": spot.rating,
        "commentCount": spot.comment_count,
        "fish": spot.fish or [],
        "pay": spot.pay,
        "cover": spot.cover,
        "coverFrom": spot.cover_from,
        "address": spot.address,
        "coord": spot.coord,
        "water": spot.water,
        "bottom": spot.bottom,
        "crowd": spot.crowd,
        "note": spot.note,
        "gallery": spot.gallery or [],
    }
    if with_comments:
        data["comments"] = [comment_out(c) for c in spot.comments]
    return data


def goods_out(goods) -> dict:
    return {
        "id": goods.id,
        "name": goods.name,
        "cat": goods.cat,
        "price": goods.price,
        "orig": goods.orig,
        "img": goods.img,
        "sales": goods.sales,
        "tags": goods.tags or [],
        "spec": goods.spec,
        "stock": goods.stock,
        "desc": goods.desc,
    }


def order_out(order) -> dict:
    return {
        "no": order.order_no,
        "goods": order.goods_name,
        "amount": order.pay_amount,
        "usePoints": order.use_points,
        "status": order.status,
        "date": order.created_at.strftime("%Y-%m-%d") if order.created_at else "",
    }


def _fmt_dt(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return dt.strftime(fmt) if dt else ""


def user_order_out(order, goods=None) -> dict:
    """小程序端订单出参：列表 / 详情共用。

    物流部分只返回公司名称、运单号、官方查询电话，不返回任何轨迹节点。
    """
    return {
        "no": order.order_no,
        "goods": order.goods_name,
        "goodsId": order.goods_id,
        "goodsImg": getattr(goods, "img", None),
        "cat": getattr(goods, "cat", None),
        "spec": getattr(goods, "spec", None),
        "qty": order.qty,
        "price": getattr(goods, "price", None),
        "total": round(order.total or 0, 2),
        "amount": round(order.pay_amount or 0, 2),
        "usePoints": order.use_points or 0,
        "status": order.status,
        "payMethod": order.pay_method or "",
        "deliveryType": order.delivery_type or "",
        "receiver": order.receiver or "",
        "receiverPhone": order.receiver_phone or "",
        "address": order.address or "",
        "logisticsCompany": order.logistics_company or "",
        "logisticsNo": order.logistics_no or "",
        "logisticsPhone": phone_of(order.logistics_company),
        "refundAmount": round(order.refund_amount or 0, 2),
        "refundStatus": order.refund_status or "none",
        "refundReason": order.refund_reason or "",
        "date": _fmt_dt(order.created_at, "%Y-%m-%d"),
        "createdAt": _fmt_dt(order.created_at),
        "paidAt": _fmt_dt(order.paid_at),
        "shippedAt": _fmt_dt(order.shipped_at),
        "completedAt": _fmt_dt(order.completed_at),
        "closedAt": _fmt_dt(order.closed_at),
    }


def address_out(a) -> dict:
    """收货地址出参：附带拼接后的完整地址，便于小程序直接展示。"""
    region = a.province + (a.city if a.city and a.city != a.province else "") + a.district
    return {
        "id": a.id,
        "receiver": a.receiver,
        "phone": a.phone,
        "province": a.province,
        "city": a.city,
        "district": a.district,
        "detail": a.detail,
        "fullAddress": region + a.detail,
        "isDefault": bool(a.is_default),
    }


def membership_order_out(order) -> dict:
    return {
        "orderNo": order.order_no,
        "plan": order.plan,
        "months": order.months,
        "payAmount": order.amount,
        "status": order.status,
    }
