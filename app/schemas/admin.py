"""schemas/admin.py — 管理后台入参校验与出参序列化

职责：
    1. 定义后台各业务（用户 / 套餐 / 商品 / 钓点 / 积分配置 / 订单 / 评论）的请求体模型；
    2. 提供 ORM 对象 → 前端 camelCase dict 的序列化函数（*_out）。

约定与 schemas.schemas 一致：入参 / 出参 camelCase，库内 snake_case。

依赖：
    - pydantic v2（BaseModel / Field / field_validator）；
    - schemas.schemas 中的 _check_phone（手机号校验）与 mask_phone（脱敏）。

注意事项：
    - 校验错误统一抛 ValueError，由 main.py 的 422 处理器转换文案；
    - 出参函数使用 getattr 兜底，允许关联对象（user / goods）为 None。
"""

from pydantic import BaseModel, Field, field_validator

from .schemas import _check_phone, mask_phone


def _check_phone_optional(v: str | None) -> str | None:
    """管理后台手机号可为空；填了就校验格式。

    Args:
        v: 原始手机号，可为 None 或空串。

    Returns:
        str | None: 空值统一返回 None，非空返回规范化后的 11 位手机号。

    Raises:
        ValueError: 非空但格式不合法（由 _check_phone 抛出）。
    """
    v = (v or "").strip()
    if not v:
        return None
    return _check_phone(v)


# ---------------- 请求体 ----------------
class AdminLoginIn(BaseModel):
    """管理员登录入参。"""

    # 管理员登录名（admins.username）
    username: str
    # 明文密码，服务端与 password_hash 比对
    password: str


class AdminUserIn(BaseModel):
    """新增小程序用户入参（后台代建账号）。"""

    # 昵称，必填，超长截断为 32 字符
    nickname: str
    # 手机号，可为空；非空时校验格式
    phone: str | None = None
    # 所在城市
    city: str | None = None
    # 账号状态：normal=正常，其他值表示停用
    status: str = "normal"

    @field_validator("nickname")
    @classmethod
    def _v_nickname(cls, v: str) -> str:
        """昵称必填校验，超长截断为 32 字符。"""
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写昵称")
        return v[:32]

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str | None) -> str | None:
        """手机号可空校验（沿用 _check_phone_optional）。"""
        return _check_phone_optional(v)


class AdminUserUpdateIn(BaseModel):
    """编辑用户入参：字段均可选，仅更新传入项。"""

    # 昵称
    nickname: str | None = None
    # 手机号，非空时校验格式
    phone: str | None = None
    # 所在城市
    city: str | None = None
    # 账号状态：normal=正常 / 其他=停用
    status: str | None = None

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str | None) -> str | None:
        """手机号可空校验（沿用 _check_phone_optional）。"""
        return _check_phone_optional(v)


class MembershipAssignIn(BaseModel):
    """后台分配 / 撤销会员入参。"""

    # 套餐 id：month / season / year / none（none 表示撤销会员，与后台种子数据一致）
    planId: str
    # 会员有效天数：1-3650 天，默认 30 天
    durationDays: int = Field(default=30, ge=1, le=3650)


class PlanIn(BaseModel):
    """新增会员套餐入参。"""

    # 套餐名称，必填，超长截断为 16 字符
    name: str
    # 有效天数：1-3650 天（与 durationLabel 展示文案配合使用）
    duration: int = Field(ge=1, le=3650)
    # 现价（元），不能为负
    price: float = Field(ge=0)
    # 划线原价（元），可为空
    origPrice: float | None = None
    # 角标文案，如「最划算」
    tag: str | None = None
    # 权益说明列表，前端逐条展示
    benefits: list[str] = []

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        """套餐名称必填校验，超长截断为 16 字符。"""
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写套餐名称")
        return v[:16]


class PlanUpdateIn(BaseModel):
    """编辑会员套餐入参：字段均可选，仅更新传入项。"""

    # 套餐名称
    name: str | None = None
    # 有效天数：1-3650 天
    duration: int | None = Field(default=None, ge=1, le=3650)
    # 现价（元），不能为负
    price: float | None = Field(default=None, ge=0)
    # 划线原价（元）
    origPrice: float | None = None
    # 角标文案
    tag: str | None = None
    # 权益说明列表
    benefits: list[str] | None = None
    # 上下架状态：on=上架 / off=下架
    status: str | None = None


class GoodsIn(BaseModel):
    """新增商品入参。"""

    # 商品名称，必填，超长截断为 64 字符
    name: str
    # 商品分类标识（如「鱼竿」「饵料」），用于列表筛选
    cat: str
    # 售价（元），不能为负
    price: float = Field(ge=0)
    # 划线原价（元），可为空
    origPrice: float | None = None
    # 库存数量，不能为负，默认 0
    stock: int = Field(default=0, ge=0)
    # 是否支持积分抵扣（前台展示与下单计算均会读取）
    pointsEnabled: bool = True
    # 上下架状态：on=上架 / off=下架
    status: str = "on"
    # 商品主图地址（上传接口返回的 /static/... 路径）
    img: str | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        """商品名称必填校验，超长截断为 64 字符。"""
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写商品名称")
        return v[:64]


class GoodsUpdateIn(BaseModel):
    """编辑商品入参：字段均可选，仅更新传入项。"""

    # 商品名称
    name: str | None = None
    # 商品分类标识
    cat: str | None = None
    # 售价（元），不能为负
    price: float | None = Field(default=None, ge=0)
    # 划线原价（元）
    origPrice: float | None = None
    # 库存数量，不能为负
    stock: int | None = Field(default=None, ge=0)
    # 是否支持积分抵扣
    pointsEnabled: bool | None = None
    # 上下架状态：on / off
    status: str | None = None
    # 商品主图地址
    img: str | None = None


class SpotIn(BaseModel):
    """新增钓点入参。"""

    # 钓点名称，必填，超长截断为 64 字符
    name: str
    # 所属城市名称（落库到 spots.region）
    city: str | None = None
    # 经纬度字符串，如 "120.15,30.28"
    coords: str | None = None
    # 距离数值，不能为负（展示单位由前端约定）
    dist: float = Field(default=0, ge=0)
    # 收费说明，如「免费」「50 元/天」
    pay: str | None = None
    # 鱼种列表
    fish: list[str] = []
    # 图集地址列表
    gallery: list[str] = []
    # 封面图地址
    cover: str | None = None
    # 上下架状态：online=上架 / offline=下架
    status: str = "online"

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        """钓点名称必填校验，超长截断为 64 字符。"""
        v = (v or "").strip()
        if not v:
            raise ValueError("请填写钓点名称")
        return v[:64]


class SpotUpdateIn(BaseModel):
    """编辑钓点入参：字段均可选，仅更新传入项。"""

    # 钓点名称
    name: str | None = None
    # 所属城市名称
    city: str | None = None
    # 经纬度字符串
    coords: str | None = None
    # 距离数值，不能为负
    dist: float | None = Field(default=None, ge=0)
    # 收费说明
    pay: str | None = None
    # 鱼种列表
    fish: list[str] | None = None
    # 图集地址列表
    gallery: list[str] | None = None
    # 封面图地址
    cover: str | None = None
    # 上下架状态：online / offline
    status: str | None = None


class PointsConfigIn(BaseModel):
    """积分配置入参（camelCase）。各字段可选，仅更新传入项。"""

    # 会员每日签到积分（0-100000）
    checkinMemberPoints: int | None = Field(default=None, ge=0, le=100000)
    # 非会员每日签到积分（0-100000）
    checkinNormalPoints: int | None = Field(default=None, ge=0, le=100000)
    # 单条广告奖励积分（0-100000）
    adPoints: int | None = Field(default=None, ge=0, le=100000)
    # 单条广告时长（秒）：5-180
    adSeconds: int | None = Field(default=None, ge=5, le=180)
    # 普通用户每日广告次数上限（0-1000）
    adLimitNormal: int | None = Field(default=None, ge=0, le=1000)
    # 会员每日广告次数上限（0-1000）
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
        """物流公司与运单号必填校验，超长截断为 64 字符。"""
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
        """收货人 / 电话 / 地址必填校验，超长截断为 255 字符。"""
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
    """管理员信息出参（登录成功 / 详情）。

    Args:
        admin: Admin ORM 对象。

    Returns:
        dict: id / username / name / role / avatarText（不含密码哈希）。
    """
    return {
        "id": admin.id,
        "username": admin.username,
        "name": admin.name,
        "role": admin.role,
        "avatarText": admin.avatar_text,
    }


# 用户未开通会员时的默认会员信息（planName/planExpire 供前端直接展示）
PLAN_DEF = {"plan": "none", "planName": "未开通", "planExpire": "—"}


def admin_user_out(user, weather_count: int = 0) -> dict:
    """后台用户列表出参。

    Args:
        user: User ORM 对象。
        weather_count: 该用户的天气查询次数（由调用方聚合统计，默认 0）。

    Returns:
        dict: 用户资料 + 会员套餐信息 + 查询次数；空值统一给中文占位符。
    """
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
    """会员套餐出参（后台列表 / 小程序会员页共用）。

    Args:
        p: Plan ORM 对象。

    Returns:
        dict: 套餐价格、有效天数、角标、销量、上下架状态与权益列表。
    """
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
    """后台商品出参（含上下架 / 积分抵扣等管理字段）。

    Args:
        g: Goods ORM 对象。

    Returns:
        dict: origPrice 由库内 orig 映射而来，pointsEnabled 统一转 bool。
    """
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
    """天气查询记录出参。

    Args:
        w: WeatherLog ORM 对象。

    Returns:
        dict: 查询地点、日期、评分/结论、气象要素、来源与身份成本。
    """
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
    """后台钓点出参。

    Args:
        s: Spot ORM 对象。

    Returns:
        dict: city 取自库内 region；createdAt 格式化为日期，空值返回 "-"。
    """
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
