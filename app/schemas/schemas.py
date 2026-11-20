"""schemas.py — 请求体校验 + 响应序列化

职责：
    1. 小程序端所有请求体的 pydantic 模型与字段级校验（手机号 / 密码 / 必填文本等）；
    2. 把 ORM 模型对象转换成前端可直接消费的 camelCase dict（*_out 系列函数）。

约定：入参与出参一律使用前端既有的 camelCase 字段名，
避免前端再做一层字段映射；数据库内部仍是 snake_case。

依赖：
    - pydantic v2（BaseModel / Field / field_validator）；
    - services.logistics.phone_of：按物流公司名称查询官方客服电话。

注意事项：
    - 校验失败抛出的 ValueError 会被 main.py 的 422 处理器转成统一响应体；
    - 出参函数只做字段挑选与脱敏，不访问数据库、不修改 ORM 对象。
"""

import re

from pydantic import BaseModel, Field, field_validator

from ..services.logistics import phone_of

# 中国大陆手机号正则：1 开头、第二位 3-9、共 11 位数字
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _check_phone(v: str) -> str:
    """手机号规范化与校验（去除空格 / 短横线 / +86 前缀）。

    Args:
        v: 原始手机号字符串，可为空。

    Returns:
        str: 规范化后的 11 位手机号。

    Raises:
        ValueError: 规范化后不符合中国大陆手机号格式。
    """
    v = (v or "").strip().replace(" ", "").replace("-", "")
    if v.startswith("+86"):
        v = v[3:]
    if not PHONE_RE.match(v):
        raise ValueError("请输入正确的 11 位手机号")
    return v


# 用户名：4-16 位，仅允许中文、英文、数字、下划线与短横线（天然排除空格与表情）
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{4,16}$")

# 表情符号检测：补充平面 Emoji + 常见 BMP 符号 + 变体选择符 / 零宽连接符
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def _check_username(v: str) -> str:
    """用户名校验：4-16 位，禁止空格与表情符号，仅允许中英文 / 数字 / _ / -。

    Args:
        v: 原始用户名。

    Returns:
        str: 原样返回合法用户名（不做去空格等隐式修改）。

    Raises:
        ValueError: 类型错误、长度越界、含空格 / 表情或非法字符。
    """
    if not isinstance(v, str):
        raise ValueError("用户名格式不正确")
    if any(ch.isspace() for ch in v):
        raise ValueError("用户名不能包含空格")
    if EMOJI_RE.search(v):
        raise ValueError("用户名不能包含表情符号")
    if not (4 <= len(v) <= 16):
        raise ValueError("用户名需为 4-16 位字符")
    if not USERNAME_RE.match(v):
        raise ValueError("用户名仅支持中英文、数字、下划线和短横线")
    return v


def _check_password(v: str) -> str:
    """密码校验：6-16 位，禁止纯空格与表情符号。

    Args:
        v: 原始密码。

    Returns:
        str: 原样返回合法密码（不加密，加密在 security.hash_password）。

    Raises:
        ValueError: 类型不是字符串、纯空格、含表情或长度越界。
    """
    if not isinstance(v, str):
        raise ValueError("密码格式不正确")
    if not v.strip():
        raise ValueError("密码不能为纯空格")
    if not (6 <= len(v) <= 16):
        raise ValueError("密码需为 6-16 位")
    if EMOJI_RE.search(v):
        raise ValueError("密码不能包含表情符号")
    return v


# ---------------- 请求体 ----------------
class WechatLoginIn(BaseModel):
    """微信小程序登录入参。"""

    # wx.login 获取的临时凭证，后端用于换取 openid（未配置微信密钥时走模拟实现）
    code: str
    # 可选昵称，首次登录时用于初始化用户资料
    nickname: str | None = None


class PasswordLoginIn(BaseModel):
    """用户名 + 密码登录：account 为用户名（4-16 位），password 为 6-16 位。"""

    # 登录账号：用户名（4-16 位，不含空格 / 表情）
    account: str
    # 明文密码：仅用于与库中哈希比对，不落库
    password: str

    @field_validator("account")
    @classmethod
    def _v_account(cls, v: str) -> str:
        """校验登录用户名的长度、字符集与非法内容。"""
        return _check_username(v)

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        """校验登录密码（6-16 位、非纯空格、不含表情）。"""
        return _check_password(v)


class RegisterIn(BaseModel):
    """注册：用户名 + 密码（6-16 位），confirmPassword 传了就校验一致。"""

    # 注册用户名，同时作为登录账号（唯一）
    username: str
    # 明文密码：6-16 位，落库前由 security.hash_password 哈希
    password: str
    # 可选确认密码：仅在传入时校验与 password 一致
    confirmPassword: str | None = None
    # 可选昵称：为空时后端使用默认昵称「钓友」
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def _v_username(cls, v: str) -> str:
        """校验并规范化注册用户名。"""
        return _check_username(v)

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str) -> str:
        """校验注册密码（6-16 位、非纯空格、不含表情）。"""
        return _check_password(v)

    @field_validator("confirmPassword")
    @classmethod
    def _v_confirm(cls, v: str | None, info) -> str | None:
        """确认密码与 password 一致（未传则不校验）。

        Args:
            v: 用户输入的确认密码。
            info: pydantic 校验上下文，可通过 info.data 读取已通过校验的 password。

        Returns:
            str | None: 原样返回确认密码。

        Raises:
            ValueError: 两次密码不一致。
        """
        if v and v != info.data.get("password"):
            raise ValueError("两次输入的密码不一致")
        return v


class ProfileIn(BaseModel):
    """修改个人资料入参：字段均为可选，只更新传入项。"""

    # 昵称，最长 32 字符（由接口层截断）
    nickname: str | None = None
    # 所在城市名称
    city: str | None = None
    # 手机号（预留绑定 / 换绑入口，需通过 _check_phone 校验；
    # 任何用户资料响应都不回传手机号，仅回传 phoneBound 布尔值）
    phone: str | None = None


class LocationIn(BaseModel):
    """设置定位入参：省 / 市 / 区三级名称。"""

    # 省 / 直辖市名称，如「浙江省」
    province: str
    # 地级市名称，如「杭州市」
    city: str
    # 区县名称，如「西湖区」
    district: str


class AddressIn(BaseModel):
    """收货地址新增 / 编辑入参。"""

    # 收货人姓名
    receiver: str
    # 收货人手机号（11 位，需通过格式校验）
    phone: str
    # 省 / 直辖市
    province: str
    # 地级市
    city: str
    # 区县
    district: str
    # 详细地址（街道门牌等）
    detail: str
    # 是否设为默认地址：同一用户最多一条为 True（业务层保证）
    isDefault: bool = False

    @field_validator("receiver", "province", "city", "district", "detail")
    @classmethod
    def _v_text(cls, v: str) -> str:
        """收货信息文本必填校验，超长截断为 255 字符。"""
        v = (v or "").strip()
        if not v:
            raise ValueError("收货信息不能为空")
        return v[:255]

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v: str) -> str:
        """校验收货人手机号格式。"""
        return _check_phone(v)


class CommentIn(BaseModel):
    """发布评论入参。"""

    # 评论正文，允许为空字符串（配合图片打分）
    text: str = ""
    # 评分：1-5 星，默认 5
    rating: int = Field(default=5, ge=1, le=5)
    # 图片地址列表（已上传到 /static 的相对路径）
    images: list[str] = []


class OrderIn(BaseModel):
    """下单入参。"""

    # 商品 id（goods 表主键）
    goodsId: str
    # 购买数量，至少 1
    qty: int = Field(default=1, ge=1)
    # 使用的积分数量，至少 0（不可能超过订单金额折算值，由业务层校验）
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

    # 套餐 id（plans 表主键），空串表示未选择
    plan: str = ""


class FavoriteIn(BaseModel):
    """收藏 / 取消收藏入参。"""

    # 野塘 id（spots 表主键）
    spotId: str


# ---------------- 响应序列化 ----------------
def _checked_today(user) -> bool:
    """判断用户今天是否已签到。

    Args:
        user: User ORM 对象。

    Returns:
        bool: last_checkin_date 等于今天则返回 True。
    """
    # 延迟导入：避免 core.utils 与本模块形成循环依赖
    from ..core.utils import today

    return user.last_checkin_date == today()


def mask_phone(phone: str | None) -> str | None:
    """手机号脱敏：13812345678 -> 138****5678

    Args:
        phone: 原始手机号，可为 None。

    Returns:
        str | None: 长度不足 7 位时原样返回（空值返回 None），否则中间四位打码。
    """
    p = (phone or "").strip()
    if len(p) < 7:
        return p or None
    return p[:3] + "****" + p[-4:]


def user_out(user) -> dict:
    """用户信息出参（“我的”页 / 登录返回共用）。

    隐私约定：不返回手机号原文或脱敏值，仅返回 username 作为身份标识，
    以及 phoneBound 布尔值供前端展示绑定状态；手机号仍保留在 users.phone。
    历史账号若 account 与 phone 相同（早期以手机号注册），username 返回空串，
    避免把手机号当作用户名变相展示。

    Args:
        user: User ORM 对象。

    Returns:
        dict: camelCase 用户资料；checkedToday 由最后签到日期推导。
    """
    account = (user.account or "").strip()
    phone = (user.phone or "").strip()
    if account and phone and account == phone:
        account = ""
    return {
        "id": user.id,
        "uid": user.uid,
        "username": account,
        "nickname": user.nickname,
        "avatarText": user.avatar_text,
        "phoneBound": bool(phone),
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
    """野塘评论出参（列表展示用）。

    Args:
        c: SpotComment ORM 对象。

    Returns:
        dict: 评论人昵称（user）、头像文字、评分、正文、日期与图片列表。
    """
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
    """野塘出参。

    Args:
        spot: Spot ORM 对象。
        with_comments: 为 True 时附带评论列表（详情页使用），默认 False。

    Returns:
        dict: 野塘基础字段；with_comments=True 时额外包含 comments 数组。
    """
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
    """商品出参（小程序商城用，隐藏上下架等后台字段）。

    Args:
        goods: Goods ORM 对象。

    Returns:
        dict: 商品基础信息，orig 为划线原价。
    """
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
    """订单简要出参（列表卡片用）。

    Args:
        order: Order ORM 对象。

    Returns:
        dict: 订单号、商品名、实付金额、使用积分、状态与下单日期。
    """
    return {
        "no": order.order_no,
        "goods": order.goods_name,
        "amount": order.pay_amount,
        "usePoints": order.use_points,
        "status": order.status,
        "date": order.created_at.strftime("%Y-%m-%d") if order.created_at else "",
    }


def _fmt_dt(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """时间格式化；空值返回空串，由前端展示为占位符。

    Args:
        dt: datetime 对象或 None。
        fmt: strftime 格式串，默认 `YYYY-MM-DD HH:MM`。

    Returns:
        str: 格式化结果或空串。
    """
    return dt.strftime(fmt) if dt else ""


def user_order_out(order, goods=None) -> dict:
    """小程序端订单出参：列表 / 详情共用。

    物流部分只返回公司名称、运单号、官方查询电话，不返回任何轨迹节点。

    Args:
        order: Order ORM 对象。
        goods: 可选 Goods ORM 对象，用于补充图片 / 分类 / 单价。

    Returns:
        dict: 订单全量字段；时间字段统一格式化为字符串，空值为空串。
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
    """收货地址出参：附带拼接后的完整地址，便于小程序直接展示。

    Args:
        a: UserAddress ORM 对象。

    Returns:
        dict: 地址各层级 + fullAddress（省市区与详细地址拼接）+ isDefault。
    """
    # 直辖市等场景 city 与 province 同名，此时只保留一次，避免出现「北京市北京市」
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
    """会员购买记录出参（小程序端「我的会员」页）。

    Args:
        order: MembershipOrder ORM 对象。

    Returns:
        dict: 订单号、套餐、购买月数、支付金额（payAmount）与状态。
    """
    return {
        "orderNo": order.order_no,
        "plan": order.plan,
        "months": order.months,
        "payAmount": order.amount,
        "status": order.status,
    }
