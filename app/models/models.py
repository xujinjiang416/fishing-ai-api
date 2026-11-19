"""models.py — 数据表定义

与前端 state.js / mock.js 的字段一一对应，命名改为 snake_case。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..core.database import Base


class User(Base):
    """用户（小程序用户 + 演示账号共用一张表）"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uid = Column(String(32), unique=True, index=True, nullable=False)
    account = Column(String(64), unique=True, index=True)
    openid = Column(String(64), unique=True, index=True)
    nickname = Column(String(64), nullable=False, default="钓友")
    avatar_text = Column(String(8), nullable=False, default="钓")
    phone = Column(String(32))
    password_hash = Column(String(200))
    level = Column(String(32), nullable=False, default="Lv.1 新手")
    city = Column(String(32))
    joined = Column(String(16))

    is_member = Column(Boolean, nullable=False, default=False)
    member_expire = Column(String(16))

    # 管理后台会员分配（prototype 契约）：plan=month/season/year/none
    plan = Column(String(16), nullable=False, default="none")
    plan_name = Column(String(32), nullable=False, default="未开通")
    plan_expire = Column(String(16))
    status = Column(String(16), nullable=False, default="normal")
    last_active = Column(String(32))

    points = Column(Integer, nullable=False, default=0)
    points_total = Column(Integer, nullable=False, default=0)
    streak = Column(Integer, nullable=False, default=0)
    max_streak = Column(Integer, nullable=False, default=0)
    last_checkin_date = Column(String(10))

    ads_date = Column(String(10))
    ads_watched_today = Column(Integer, nullable=False, default=0)
    ad_points_today = Column(Integer, nullable=False, default=0)

    location = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

    favorites = relationship(
        "Favorite", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class Province(Base):
    __tablename__ = "provinces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(32), unique=True, index=True, nullable=False)
    sort = Column(Integer, nullable=False, default=0)

    cities = relationship(
        "City",
        back_populates="province",
        cascade="all, delete-orphan",
        order_by="City.id",
        lazy="selectin",
    )


class City(Base):
    __tablename__ = "cities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    province_id = Column(Integer, ForeignKey("provinces.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(32), nullable=False)
    sort = Column(Integer, nullable=False, default=0)

    province = relationship("Province", back_populates="cities")
    districts = relationship(
        "District",
        back_populates="city",
        cascade="all, delete-orphan",
        order_by="District.id",
        lazy="selectin",
    )


class District(Base):
    __tablename__ = "districts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    sort = Column(Integer, nullable=False, default=0)

    city = relationship("City", back_populates="districts")


class Spot(Base):
    """野塘"""

    __tablename__ = "spots"

    id = Column(String(16), primary_key=True)
    name = Column(String(64), nullable=False)
    # 所属城市（如「杭州市」）：按位置查询时用于归属与过滤
    region = Column(String(32), index=True)
    # 最近一次由大模型生成的时间，超过 10 天则下次查询时重新生成
    refreshed_at = Column(DateTime, default=datetime.now)
    dist = Column(Float, nullable=False, default=0)
    walk = Column(String(32))
    rating = Column(Float, nullable=False, default=0)
    comment_count = Column(Integer, nullable=False, default=0)
    fish = Column(JSON, nullable=False, default=list)
    pay = Column(String(32))
    cover = Column(String(255))
    cover_from = Column(String(16))
    address = Column(String(255))
    coord = Column(String(64))
    water = Column(String(64))
    bottom = Column(String(32))
    crowd = Column(String(32))
    note = Text
    gallery = Column(JSON, nullable=False, default=list)
    # 管理后台字段：上下架（online/offline）/ 添加人 / 创建时间
    status = Column(String(16), nullable=False, default="online")
    added_by = Column(String(64))
    created_at = Column(DateTime)

    comments = relationship(
        "SpotComment",
        back_populates="spot",
        cascade="all, delete-orphan",
        order_by="SpotComment.created_at.desc()",
        lazy="selectin",
    )


class SpotComment(Base):
    __tablename__ = "spot_comments"

    id = Column(String(24), primary_key=True)
    spot_id = Column(String(16), ForeignKey("spots.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    user_name = Column(String(64), nullable=False)
    avatar = Column(String(8))
    rating = Column(Integer, nullable=False, default=5)
    text = Column(Text, nullable=False, default="")
    date = Column(String(32))
    images = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.now)

    spot = relationship("Spot", back_populates="comments")


class Goods(Base):
    __tablename__ = "goods"

    id = Column(String(16), primary_key=True)
    name = Column(String(128), nullable=False)
    cat = Column(String(32), nullable=False, index=True)
    price = Column(Float, nullable=False, default=0)
    orig = Column(Float)
    img = Column(String(255))
    sales = Column(String(32))
    tags = Column(JSON, nullable=False, default=list)
    spec = Column(String(128))
    stock = Column(Integer, nullable=False, default=0)
    desc = Column(Text)
    # 管理后台字段：积分抵扣 / 上下架 / 更新时间
    points_enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(8), nullable=False, default="on")
    updated = Column(String(16))


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_no = Column(String(32), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    goods_id = Column(String(16), index=True)
    goods_name = Column(String(128))
    qty = Column(Integer, nullable=False, default=1)
    total = Column(Float, nullable=False, default=0)
    use_points = Column(Integer, nullable=False, default=0)
    pay_amount = Column(Float, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="待发货")
    created_at = Column(DateTime, default=datetime.now)


class MembershipOrder(Base):
    __tablename__ = "membership_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_no = Column(String(32), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(32), nullable=False, default="monthly")
    months = Column(Integer, nullable=False, default=1)
    amount = Column(Float, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="已支付")
    created_at = Column(DateTime, default=datetime.now)


class CheckinRecord(Base):
    __tablename__ = "checkin_records"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_checkin_user_date"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(String(10), nullable=False)
    points = Column(Integer, nullable=False, default=0)
    streak = Column(Integer, nullable=False, default=0)
    bonus = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.now)


class AdReward(Base):
    __tablename__ = "ad_rewards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(String(10), nullable=False)
    points = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.now)


class PointsLog(Base):
    __tablename__ = "points_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    change = Column(Integer, nullable=False, default=0)
    reason = Column(String(32), nullable=False, default="")
    balance = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.now)


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "spot_id", name="uq_favorite_user_spot"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    spot_id = Column(String(16), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="favorites")


class WeatherCache(Base):
    """天气 + 鱼情研判缓存：同一地区同一天只让大模型算一次"""

    __tablename__ = "weather_cache"
    __table_args__ = (UniqueConstraint("region", "day", name="uq_weather_region_day"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 地区（城市名，如「杭州市」；无定位时用「默认」）
    region = Column(String(32), nullable=False, index=True)
    # 查询日期 YYYY-MM-DD
    day = Column(String(16), nullable=False)
    # 大模型产出的 weather / verdict / slots 完整快照
    payload = Column(JSON, nullable=False)
    # 数据来源：qweather / llm / fallback
    source = Column(String(16), nullable=False, default="llm")
    # 过期时间（由缓存过期规则按地区等级/精度计算）
    expire_at = Column(DateTime, nullable=True)
    # 缓存占用字节数（payload 序列化后估算）
    size_bytes = Column(Integer, nullable=False, default=0)
    # 命中次数（命中缓存返回的次数）
    hit_count = Column(Integer, nullable=False, default=0)
    # 最近一次命中时间
    last_hit_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class Admin(Base):
    """管理后台账号（与小程序用户 users 表分开）"""

    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    name = Column(String(64), nullable=False, default="系统管理员")
    role = Column(String(32), nullable=False, default="超级管理员")
    avatar_text = Column(String(8), nullable=False, default="渔")
    status = Column(String(16), nullable=False, default="normal")
    created_at = Column(DateTime, default=datetime.now)


class Plan(Base):
    """会员套餐（月卡 / 季卡 / 年卡，管理后台可配置）"""

    __tablename__ = "plans"

    id = Column(String(16), primary_key=True)
    name = Column(String(32), nullable=False)
    duration = Column(Integer, nullable=False, default=30)
    duration_label = Column(String(16), nullable=False, default="30 天")
    price = Column(Float, nullable=False, default=0)
    orig_price = Column(Float)
    icon = Column(String(16), nullable=False, default="crown")
    tag = Column(String(32))
    tag_type = Column(String(16), nullable=False, default="primary")
    sales = Column(Integer, nullable=False, default=0)
    status = Column(String(8), nullable=False, default="off")
    sort = Column(Integer, nullable=False, default=0)
    benefits = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.now)


class WeatherLog(Base):
    """天气查询日志：小程序每次查天气落一条，供管理后台查看 / 重新生成"""

    __tablename__ = "weather_logs"

    id = Column(String(16), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    user = Column(String(64), nullable=False, default="游客")
    place = Column(String(128))
    place_detail = Column(String(128))
    date = Column(String(32))
    score = Column(Integer, nullable=False, default=0)
    verdict = Column(String(16))
    level = Column(String(16))
    condition = Column(String(32))
    temp = Column(Float)
    pressure = Column(Float)
    wind_diff = Column(String(32))
    # source：手动选点 / 手机定位
    # cost：本次查询的身份成本 —— 会员 / 普通用户（普通用户不再有广告/付费解锁，统一记为「普通用户」）
    source = Column(String(16), nullable=False, default="手动选点")
    cost = Column(String(16), nullable=False, default="普通用户")
    # 本次查询是否命中缓存（用于缓存命中率 / 命中趋势统计）
    cache_hit = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now)


class AppConfig(Base):
    """系统配置表（键值对）。

    用于存放「运行时可变」的业务规则，例如：签到积分（会员 / 非会员）、
    观看广告单次积分、广告时长与每日额度等。管理后台「系统设置 · 积分配置」
    修改后写入本表，小程序端通过公开接口 GET /api/config 实时读取，
    做到「后台改配置 → 小程序无需发版即可生效」。
    """

    __tablename__ = "app_configs"

    # 配置项键名（主键），如 checkin_member_points / ad_points
    key = Column(String(48), primary_key=True)
    # 配置值：统一以字符串存储，读取时按业务类型转型（避免不同 DB 的数值差异）
    value = Column(String(255), nullable=False, default="")
    # 配置项说明（供后台展示，便于运营理解含义）
    remark = Column(String(128))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class OperationLog(Base):
    """管理后台操作日志。

    记录「配置变更 / 评论删除」等关键管理动作，满足审计与全链路追溯要求。
    仅追加、不删除，管理员可在「系统设置 · 操作日志」中按关键词 / 动作类型检索。
    """

    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 操作管理员（可能为空：进程内系统任务）
    admin_id = Column(Integer)
    admin_name = Column(String(64), nullable=False, default="系统管理员")
    # 动作类型：config.update / comment.delete / comment.batch_delete 等，便于筛选
    action = Column(String(32), nullable=False, index=True)
    # 操作对象（如配置键、评论 id、钓点 id）
    target = Column(String(128))
    # 人类可读的变更详情（中文描述，便于审计）
    detail = Column(Text)
    # 来源 IP（尽力而为，取不到则为空）
    ip = Column(String(64))
    created_at = Column(DateTime, default=datetime.now, index=True)

