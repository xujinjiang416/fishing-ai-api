"""schemas.py — 请求体校验 + 响应序列化

约定：入参与出参一律使用前端既有的 camelCase 字段名，
避免前端再做一层字段映射；数据库内部仍是 snake_case。
"""

import re

from pydantic import BaseModel, Field, field_validator

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


class CommentIn(BaseModel):
    text: str = ""
    rating: int = Field(default=5, ge=1, le=5)
    images: list[str] = []


class OrderIn(BaseModel):
    goodsId: str
    qty: int = Field(default=1, ge=1)
    usePoints: int = Field(default=0, ge=0)


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


def membership_order_out(order) -> dict:
    return {
        "orderNo": order.order_no,
        "plan": order.plan,
        "months": order.months,
        "payAmount": order.amount,
        "status": order.status,
    }
