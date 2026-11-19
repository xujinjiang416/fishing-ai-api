"""schemas/admin.py — 管理后台入参校验与出参序列化

约定与 schemas.schemas 一致：入参 / 出参 camelCase，库内 snake_case。
"""

from pydantic import BaseModel, Field, field_validator

from .schemas import _check_phone, mask_phone


def _check_phone_optional(v: str | None) -> str | None:
    """管理后台手机号可为空；填了就校验格式。"""
    v = (v or "").strip()
    if not v:
        return None
    return _check_phone(v)


# ---------------- 请求体 ----------------
class AdminLoginIn(BaseModel):
    username: str
    password: str


class AdminUserIn(BaseModel):
    nickname: str
    phone: str | None = None
    city: str | None = None
    status: str = "normal"

    @field_validator("nickname")
    @classmethod
    def _v_nickname(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写昵称")
        return v[:32]

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str | None) -> str | None:
        return _check_phone_optional(v)


class AdminUserUpdateIn(BaseModel):
    nickname: str | None = None
    phone: str | None = None
    city: str | None = None
    status: str | None = None

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str | None) -> str | None:
        return _check_phone_optional(v)


class MembershipAssignIn(BaseModel):
    planId: str
    durationDays: int = Field(default=30, ge=1, le=3650)


class PlanIn(BaseModel):
    name: str
    duration: int = Field(ge=1, le=3650)
    price: float = Field(ge=0)
    origPrice: float | None = None
    tag: str | None = None
    benefits: list[str] = []

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写套餐名称")
        return v[:16]


class PlanUpdateIn(BaseModel):
    name: str | None = None
    duration: int | None = Field(default=None, ge=1, le=3650)
    price: float | None = Field(default=None, ge=0)
    origPrice: float | None = None
    tag: str | None = None
    benefits: list[str] | None = None
    status: str | None = None


class GoodsIn(BaseModel):
    name: str
    cat: str
    price: float = Field(ge=0)
    origPrice: float | None = None
    stock: int = Field(default=0, ge=0)
    pointsEnabled: bool = True
    status: str = "on"
    img: str | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写商品名称")
        return v[:64]


class GoodsUpdateIn(BaseModel):
    name: str | None = None
    cat: str | None = None
    price: float | None = Field(default=None, ge=0)
    origPrice: float | None = None
    stock: int | None = Field(default=None, ge=0)
    pointsEnabled: bool | None = None
    status: str | None = None
    img: str | None = None


class SpotIn(BaseModel):
    name: str
    city: str | None = None
    coords: str | None = None
    dist: float = Field(default=0, ge=0)
    pay: str | None = None
    fish: list[str] = []
    gallery: list[str] = []
    cover: str | None = None
    status: str = "online"

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写钓点名称")
        return v[:64]


class SpotUpdateIn(BaseModel):
    name: str | None = None
    city: str | None = None
    coords: str | None = None
    dist: float | None = Field(default=None, ge=0)
    pay: str | None = None
    fish: list[str] | None = None
    gallery: list[str] | None = None
    cover: str | None = None
    status: str | None = None


class PointsConfigIn(BaseModel):
    """积分配置入参（camelCase）。各字段可选，仅更新传入项。"""

    checkinMemberPoints: int | None = Field(default=None, ge=0, le=100000)
    checkinNormalPoints: int | None = Field(default=None, ge=0, le=100000)
    adPoints: int | None = Field(default=None, ge=0, le=100000)
    adSeconds: int | None = Field(default=None, ge=5, le=180)
    adLimitNormal: int | None = Field(default=None, ge=0, le=1000)
    adLimitMember: int | None = Field(default=None, ge=0, le=1000)
    # 评论敏感词（英文逗号分隔），留空表示清空自定义词库（回退内置词库）
    sensitiveWords: str | None = None


class BatchDeleteCommentsIn(BaseModel):
    """批量删除评论：ids 为评论 id 列表。"""

    ids: list[str] = Field(default_factory=list)


class BatchIdsIn(BaseModel):
    """通用批量删除入参：ids 为资源 id（或 uid）列表。"""

    ids: list[str] = Field(default_factory=list)


class CacheBatchIn(BaseModel):
    """批量操作天气缓存：ids 为缓存自增 id 列表。"""

    ids: list[int] = Field(default_factory=list)


class CacheRulesIn(BaseModel):
    """缓存有效期规则入参：天气 / 鱼情 TTL（秒），默认 3 小时。"""

    weather: int = Field(default=10800, ge=60)
    fish: int = Field(default=10800, ge=60)


# ---------------- 订单管理 ----------------
class OrderShipIn(BaseModel):
    """发货：录入物流公司 / 单号，可选覆盖配送方式。"""

    logisticsCompany: str
    logisticsNo: str
    deliveryType: str | None = None

    @field_validator("logisticsCompany", "logisticsNo")
    @classmethod
    def _v_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("物流公司与运单号不能为空")
        return v[:64]


class OrderAddressIn(BaseModel):
    """修改收货地址（已发货订单也可修改）。"""

    receiver: str
    receiverPhone: str
    address: str

    @field_validator("receiver", "receiverPhone", "address")
    @classmethod
    def _v_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("收货人、电话与地址不能为空")
        return v[:255]


class OrderRemarkIn(BaseModel):
    """订单备注 / 异常标记。"""

    remark: str | None = None
    abnormal: bool | None = None


class OrderRefundIn(BaseModel):
    """退款 / 部分退款处理：approve=false 表示驳回申请。"""

    amount: float = Field(default=0, ge=0)
    reason: str | None = None
    approve: bool = True


# ---------------- 响应序列化 ----------------
def admin_out(admin) -> dict:
    return {
        "id": admin.id,
        "username": admin.username,
        "name": admin.name,
        "role": admin.role,
        "avatarText": admin.avatar_text,
    }


PLAN_DEF = {"plan": "none", "planName": "未开通", "planExpire": "—"}


def admin_user_out(user, weather_count: int = 0) -> dict:
    return {
        "uid": user.uid,
        "nickname": user.nickname,
        "avatar": user.avatar_text,
        "phone": user.phone,
        "city": user.city,
        "points": user.points,
        "streak": user.streak,
        "plan": user.plan or "none",
        "planName": user.plan_name or "未开通",
        "planExpire": user.plan_expire or "—",
        "joined": user.joined,
        "lastActive": user.last_active or "—",
        "status": user.status or "normal",
        "weatherCount": weather_count,
    }


def plan_out(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "duration": p.duration,
        "durationLabel": p.duration_label,
        "price": p.price,
        "origPrice": p.orig_price,
        "icon": p.icon,
        "tag": p.tag,
        "tagType": p.tag_type,
        "sales": p.sales,
        "status": p.status,
        "sort": p.sort,
        "benefits": p.benefits or [],
    }


def goods_admin_out(g) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "cat": g.cat,
        "price": g.price,
        "origPrice": g.orig,
        "stock": g.stock,
        "sales": g.sales,
        "img": g.img,
        "pointsEnabled": bool(g.points_enabled),
        "status": g.status or "on",
        "updated": g.updated or "",
    }


def weather_log_out(w) -> dict:
    return {
        "id": w.id,
        "user": w.user,
        "place": w.place,
        "placeDetail": w.place_detail,
        "date": w.date,
        "score": w.score,
        "verdict": w.verdict,
        "level": w.level,
        "condition": w.condition,
        "temp": w.temp,
        "pressure": w.pressure,
        "windDiff": w.wind_diff,
        "source": w.source,
        "cost": w.cost,
    }


def spot_admin_out(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "city": s.region,
        "dist": s.dist,
        "rating": s.rating,
        "commentCount": s.comment_count,
        "fish": s.fish or [],
        "pay": s.pay,
        "coords": s.coord,
        "status": s.status or "online",
        "cover": s.cover,
        "gallery": s.gallery or [],
        "addedBy": s.added_by or "系统管理员",
        "createdAt": s.created_at.strftime("%Y-%m-%d") if s.created_at else "-",
    }


def admin_comment_out(c, spot_name: str | None = None) -> dict:
    """评论管理出参：评论人信息 / 关联内容 / 发布时间 / 评论正文。"""
    return {
        "id": c.id,
        "userId": c.user_id,
        "user": c.user_name,
        "avatar": c.avatar or (c.user_name or "钓")[:1],
        "spotId": c.spot_id,
        "spot": spot_name or c.spot_id,
        "rating": c.rating,
        "text": c.text,
        "images": c.images or [],
        "date": c.date or "",
        "createdAt": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "",
    }


def operation_log_out(log) -> dict:
    """操作日志出参。"""
    return {
        "id": log.id,
        "adminId": log.admin_id,
        "admin": log.admin_name,
        "action": log.action,
        "target": log.target,
        "detail": log.detail,
        "ip": log.ip,
        "createdAt": log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "",
    }


def membership_order_admin_out(order, user_name: str | None = None) -> dict:
    """会员购买记录出参（含用户昵称）。"""
    return {
        "orderNo": order.order_no,
        "user": user_name or "",
        "plan": order.plan,
        "months": order.months,
        "amount": order.amount,
        "status": order.status,
        "date": order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "",
    }


# ---------------- 订单管理 ----------------
def _dt(dt, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """时间格式化；空值统一返回空串，由前端展示为「--」。"""
    return dt.strftime(fmt) if dt else ""


def _pay_status(order) -> str:
    """由订单状态推导支付状态：待支付=未支付，已关闭未支付单=已关闭，其余=已支付。"""
    if order.status == "待支付":
        return "未支付"
    if order.status == "已关闭":
        return "已关闭"
    if order.refund_amount and order.status in ("已退款", "部分退款"):
        return "已退款"
    return "已支付"


def admin_order_out(order, user=None, goods=None) -> dict:
    """订单列表出参：订单核心字段 + 用户 / 商品摘要。"""
    return {
        "no": order.order_no,
        "userId": order.user_id,
        "uid": getattr(user, "uid", None) or "",
        "user": getattr(user, "nickname", None) or "—",
        "userAvatar": getattr(user, "avatar_text", None) or "钓",
        "userPhone": mask_phone(getattr(user, "phone", None)) or "—",
        "goodsId": order.goods_id or "",
        "goods": order.goods_name or "—",
        "goodsImg": getattr(goods, "img", None),
        "qty": order.qty,
        "total": round(order.total or 0, 2),
        "usePoints": order.use_points or 0,
        "amount": round(order.pay_amount or 0, 2),
        "payMethod": order.pay_method or "—",
        "payStatus": _pay_status(order),
        "status": order.status,
        "deliveryType": order.delivery_type or "—",
        "receiver": order.receiver or "",
        "receiverPhone": order.receiver_phone or "",
        "address": order.address or "",
        "logisticsCompany": order.logistics_company or "",
        "logisticsNo": order.logistics_no or "",
        "refundAmount": round(order.refund_amount or 0, 2),
        "refundStatus": order.refund_status or "none",
        "refundReason": order.refund_reason or "",
        "abnormal": bool(order.abnormal),
        "remark": order.remark or "",
        "createdAt": _dt(order.created_at),
        "paidAt": _dt(order.paid_at),
        "shippedAt": _dt(order.shipped_at),
        "completedAt": _dt(order.completed_at),
        "closedAt": _dt(order.closed_at),
    }


def admin_order_detail_out(order, user=None, goods=None, logs=None) -> dict:
    """订单详情出参：列表字段 + 商品明细 / 用户 / 操作日志。"""
    data = admin_order_out(order, user, goods)
    price = getattr(goods, "price", None)
    data["goodsDetail"] = {
        "id": order.goods_id or "",
        "name": order.goods_name or "—",
        "cat": getattr(goods, "cat", None) or "—",
        "img": getattr(goods, "img", None),
        "spec": getattr(goods, "spec", None) or "—",
        "price": round(price, 2) if price is not None else round(order.total / (order.qty or 1), 2),
        "qty": order.qty,
        "subtotal": round(order.total or 0, 2),
    }
    data["userInfo"] = {
        "uid": getattr(user, "uid", None) or "",
        "nickname": getattr(user, "nickname", None) or "—",
        "phone": mask_phone(getattr(user, "phone", None)) or "—",
        "city": getattr(user, "city", None) or "—",
        "isMember": bool(getattr(user, "is_member", False)),
    }
    data["logs"] = [
        {
            "action": log.action,
            "operator": log.operator,
            "detail": log.detail or "",
            "createdAt": _dt(log.created_at),
        }
        for log in (logs or [])
    ]
    return data
