"""schemas/admin.py — 管理后台入参校验与出参序列化

约定与 schemas.schemas 一致：入参 / 出参 camelCase，库内 snake_case。
"""

from pydantic import BaseModel, Field, field_validator

from .schemas import _check_phone


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
    """缓存过期规则入参：城市等级 / 数据精度两维 TTL（秒）。"""

    cityLevels: dict[str, int] = Field(default_factory=dict)
    precisions: dict[str, int] = Field(default_factory=dict)


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
