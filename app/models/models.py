"""models.py — 数据表定义

职责：使用 SQLAlchemy 2.x 声明式 API 定义全部业务表，并声明表间外键与 ORM 关系。

关键流程：
    各模型继承 core.database.Base → 应用启动时由 seed.run_seed 调用 create_all 建表
    → 路由通过 AsyncSession 以 ORM 方式读写。

约定：
    - 字段命名 snake_case，与前端 state.js / mock.js 的 camelCase 通过 schemas 转换；
    - 时间列统一使用 DateTime（本地时区），日期类字段为 `YYYY-MM-DD` 字符串；
    - 结构化数据（鱼种、图集、快照等）使用 MySQL JSON 列存储。

依赖：core.database.Base（声明式基类）、sqlalchemy（Column / relationship / ForeignKey）。

注意事项：
    - 外键使用 ondelete 语义：CASCADE = 主体删除时从属数据一并删除，
      SET NULL = 主体删除后保留从属数据但清空外键；
    - 列表类 JSON 字段默认值为 list，避免 None 参与序列化；
    - 新增字段需同步检查 schemas 出参、seed 初始化数据与管理后台表单。
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

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    uid = Column(String(32), unique=True, index=True, nullable=False)  # 业务用户号（展示 / 接口用）
    account = Column(String(64), unique=True, index=True)  # 账号字段（演示账号等使用；常规登录按 phone 匹配）
    openid = Column(String(64), unique=True, index=True)  # 微信 openid，未绑定微信时为空
    nickname = Column(String(64), nullable=False, default="钓友")  # 昵称，默认「钓友」
    avatar_text = Column(String(8), nullable=False, default="钓")  # 头像占位文字（取昵称首字），默认「钓」
    phone = Column(String(32))  # 手机号（明文存储，出参统一脱敏）
    password_hash = Column(String(200))  # 密码哈希（security.hash_password 生成的字符串）
    level = Column(String(32), nullable=False, default="Lv.1 新手")  # 等级文案，默认 Lv.1 新手
    city = Column(String(32))  # 所在城市名称
    joined = Column(String(16))  # 注册日期 `YYYY-MM-DD`，用于资料页展示

    is_member = Column(Boolean, nullable=False, default=False)  # 是否有效会员（支付 / 后台分配后置 True）
    member_expire = Column(String(16))  # 会员到期日 `YYYY-MM-DD`

    # 管理后台会员分配（prototype 契约）：plan=month/season/year/none
    plan = Column(String(16), nullable=False, default="none")  # 套餐标识：month/season/year/none
    plan_name = Column(String(32), nullable=False, default="未开通")  # 套餐展示名，默认「未开通」
    plan_expire = Column(String(16))  # 套餐到期日 `YYYY-MM-DD`
    status = Column(String(16), nullable=False, default="normal")  # 账号状态：normal=正常，其他=停用
    last_active = Column(String(32))  # 最近活跃时间文案，后台列表展示用

    points = Column(Integer, nullable=False, default=0)  # 当前可用积分余额，默认 0
    points_total = Column(Integer, nullable=False, default=0)  # 历史累计获得积分，默认 0
    streak = Column(Integer, nullable=False, default=0)  # 当前连续签到天数，默认 0
    max_streak = Column(Integer, nullable=False, default=0)  # 历史最长连续签到天数，默认 0
    last_checkin_date = Column(String(10))  # 最后签到日期 `YYYY-MM-DD`，用于判断今日是否已签到

    ads_date = Column(String(10))  # 广告计数所属日期 `YYYY-MM-DD`，跨天时清零计数
    ads_watched_today = Column(Integer, nullable=False, default=0)  # 今日已看广告条数，默认 0
    ad_points_today = Column(Integer, nullable=False, default=0)  # 今日广告所得积分，默认 0

    location = Column(JSON)  # 定位信息（province/city/district），供天气与野塘查询使用
    created_at = Column(DateTime, default=datetime.now)  # 注册时间

    # 一对多：用户收藏的野塘；用户删除时级联删除收藏记录，查询时预加载
    favorites = relationship(
        "Favorite", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class Province(Base):
    """省级行政区（省 / 直辖市 / 自治区 / 特别行政区）。"""

    __tablename__ = "provinces"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    name = Column(String(32), unique=True, index=True, nullable=False)  # 省名（唯一），如「浙江省」
    sort = Column(Integer, nullable=False, default=0)  # 排序权重，升序展示，默认 0

    # 一对多：省下的城市；删除省时级联删除城市，按 id 升序返回
    cities = relationship(
        "City",
        back_populates="province",
        cascade="all, delete-orphan",
        order_by="City.id",
        lazy="selectin",
    )


class City(Base):
    """地级市（隶属某个省，下辖区县）。"""

    __tablename__ = "cities"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    province_id = Column(Integer, ForeignKey("provinces.id", ondelete="CASCADE"), nullable=False, index=True)  # 所属省 id；省删除时级联删除
    name = Column(String(32), nullable=False)  # 市名，如「杭州市」（同省内唯一由业务保证）
    sort = Column(Integer, nullable=False, default=0)  # 排序权重，升序展示，默认 0

    # 多对一：所属省（province.cities 的反向引用）
    province = relationship("Province", back_populates="cities")
    # 一对多：市下辖区县；删除市时级联删除区县
    districts = relationship(
        "District",
        back_populates="city",
        cascade="all, delete-orphan",
        order_by="District.id",
        lazy="selectin",
    )


class District(Base):
    """区 / 县（隶属于某个市，是天气与野塘查询的最小行政单位）。"""

    __tablename__ = "districts"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    city_id = Column(Integer, ForeignKey("cities.id", ondelete="CASCADE"), nullable=False, index=True)  # 所属市 id；市删除时级联删除
    name = Column(String(64), nullable=False)  # 区县名，如「西湖区」
    sort = Column(Integer, nullable=False, default=0)  # 排序权重，升序展示，默认 0

    # 多对一：所属市（city.districts 的反向引用）
    city = relationship("City", back_populates="districts")


class Spot(Base):
    """野塘"""

    __tablename__ = "spots"

    id = Column(String(16), primary_key=True)  # 业务主键（字符串，由生成逻辑分配）
    name = Column(String(64), nullable=False)  # 钓点名称
    # 所属城市（如「杭州市」）：按位置查询时用于归属与过滤
    region = Column(String(32), index=True)
    # 最近一次由大模型生成的时间，超过 10 天则下次查询时重新生成
    refreshed_at = Column(DateTime, default=datetime.now)
    dist = Column(Float, nullable=False, default=0)  # 与用户的距离数值（展示单位由前端约定），默认 0
    walk = Column(String(32))  # 步行耗时文案，如「步行 15 分钟」
    rating = Column(Float, nullable=False, default=0)  # 综合评分（0-5），默认 0
    comment_count = Column(Integer, nullable=False, default=0)  # 评论条数（冗余统计），默认 0
    fish = Column(JSON, nullable=False, default=list)  # 鱼种列表（字符串数组），默认空列表
    pay = Column(String(32))  # 收费说明，如「免费」「50 元/天」
    cover = Column(String(255))  # 封面图地址
    cover_from = Column(String(16))  # 封面来源标识（如 llm / upload），用于追溯
    address = Column(String(255))  # 详细地址文案
    coord = Column(String(64))  # 经纬度字符串，如 "120.15,30.28"
    water = Column(String(64))  # 水域类型描述，如「水库」「野河」
    bottom = Column(String(32))  # 水底地形描述，如「泥底」「沙底」
    crowd = Column(String(32))  # 人流情况描述，如「较少」
    note = Text  # 钓点补充说明（长文本）
    gallery = Column(JSON, nullable=False, default=list)  # 图集地址列表，默认空列表
    # 管理后台字段：上下架（online/offline）/ 添加人 / 创建时间
    status = Column(String(16), nullable=False, default="online")
    added_by = Column(String(64))  # 添加人（后台账号名），系统生成时为「系统管理员」
    created_at = Column(DateTime)  # 创建时间（后台录入时间，可为空）

    # 一对多：钓点评论；删除钓点时级联删除评论，按创建时间倒序返回
    comments = relationship(
        "SpotComment",
        back_populates="spot",
        cascade="all, delete-orphan",
        order_by="SpotComment.created_at.desc()",
        lazy="selectin",
    )


class SpotComment(Base):
    """野塘评论（用户对某个钓点的评分与图文评价）。"""

    __tablename__ = "spot_comments"

    id = Column(String(24), primary_key=True)  # 业务主键（字符串短 id）
    spot_id = Column(String(16), ForeignKey("spots.id", ondelete="CASCADE"), nullable=False, index=True)  # 所属钓点 id；钓点删除时级联删除
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))  # 评论用户 id；用户删除后置空（保留评论）
    user_name = Column(String(64), nullable=False)  # 评论人昵称快照（避免用户改名 / 删除后显示为空）
    avatar = Column(String(8))  # 头像文字快照（取昵称首字）
    rating = Column(Integer, nullable=False, default=5)  # 评分 1-5 星，默认 5
    text = Column(Text, nullable=False, default="")  # 评论正文，默认空串
    date = Column(String(32))  # 展示用日期文案（如「2026-11-05」）
    images = Column(JSON, nullable=False, default=list)  # 评论图片地址列表，默认空列表
    created_at = Column(DateTime, default=datetime.now)  # 创建时间（排序依据）

    # 多对一：所属钓点（spot.comments 的反向引用）
    spot = relationship("Spot", back_populates="comments")


class Goods(Base):
    """商城商品（渔具 / 饵料等，后台录入并可上下架）。"""

    __tablename__ = "goods"

    id = Column(String(16), primary_key=True)  # 业务主键（字符串）
    name = Column(String(128), nullable=False)  # 商品名称
    cat = Column(String(32), nullable=False, index=True)  # 分类标识（如「鱼竿」「饵料」），用于筛选
    price = Column(Float, nullable=False, default=0)  # 现价（元），默认 0
    orig = Column(Float)  # 划线原价（元），可为空
    img = Column(String(255))  # 主图地址（/static/...）
    sales = Column(String(32))  # 销量展示文案，如「已售 120」
    tags = Column(JSON, nullable=False, default=list)  # 标签列表（字符串数组），默认空列表
    spec = Column(String(128))  # 规格描述，如「3.6 米 / 硬调」
    stock = Column(Integer, nullable=False, default=0)  # 库存数量，默认 0
    desc = Column(Text)  # 商品详情（长文本）
    # 管理后台字段：积分抵扣 / 上下架 / 更新时间
    points_enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(8), nullable=False, default="on")  # 上下架：on=上架 / off=下架
    updated = Column(String(16))  # 最近更新时间文案（后台展示用）


class Order(Base):
    """商城订单（实物商品订单，含支付 / 物流 / 退款全流程字段）。"""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    order_no = Column(String(32), unique=True, index=True, nullable=False)  # 订单号（唯一，见 utils.gen_no）
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 下单用户；用户删除时级联删除订单
    goods_id = Column(String(16), index=True)  # 商品 id（无外键，商品删除后订单仍可展示快照）
    goods_name = Column(String(128))  # 商品名称快照
    qty = Column(Integer, nullable=False, default=1)  # 购买数量，默认 1
    total = Column(Float, nullable=False, default=0)  # 商品总额（元）= 单价 × 数量
    use_points = Column(Integer, nullable=False, default=0)  # 使用的积分数量，默认 0
    pay_amount = Column(Float, nullable=False, default=0)  # 实付金额（元）= 总额 - 积分抵扣
    status = Column(String(16), nullable=False, default="待发货")  # 订单状态：待支付/待发货/已发货/已完成/已关闭 等

    # ---- 支付 / 配送 / 收货（管理后台订单管理） ----
    pay_method = Column(String(16), nullable=False, default="微信支付")  # 支付方式，默认微信支付
    delivery_type = Column(String(16), nullable=False, default="快递")  # 配送方式：快递 / 自提
    receiver = Column(String(32))  # 收货人姓名（下单时从地址簿快照）
    receiver_phone = Column(String(32))  # 收货人手机号
    address = Column(String(255))  # 收货地址全文快照
    logistics_company = Column(String(32))  # 物流公司名称（发货时录入）
    logistics_no = Column(String(64))  # 物流运单号（发货时录入）
    # ---- 运营备注 / 异常标记 / 退款 ----
    remark = Column(Text)  # 运营备注
    abnormal = Column(Boolean, nullable=False, default=False)  # 是否标记为异常订单，默认 False
    refund_amount = Column(Float, nullable=False, default=0)  # 已退款金额（元），支持部分退款，默认 0
    refund_status = Column(String(16), nullable=False, default="none")  # 退款状态：none/pending/approved/rejected 等
    refund_reason = Column(Text)  # 退款原因（用户申请或运营填写）
    # ---- 状态时间戳 ----
    paid_at = Column(DateTime)  # 支付完成时间
    shipped_at = Column(DateTime)  # 发货时间
    completed_at = Column(DateTime)  # 完成时间
    closed_at = Column(DateTime)  # 关闭时间
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)  # 最近更新时间（自动维护）
    created_at = Column(DateTime, default=datetime.now)  # 下单时间


class OrderLog(Base):
    """订单操作日志：记录每一次后台/系统对订单的操作，用于订单详情全链路追溯。

    order_no 与订单号关联（不使用外键，便于订单归档后仍保留日志）。
    """

    __tablename__ = "order_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    order_no = Column(String(32), nullable=False, index=True)  # 关联订单号（无外键，日志独立留存）
    # 动作：create / pay / close / ship / address / remark / refund / complete
    action = Column(String(32), nullable=False, default="")
    operator = Column(String(64), nullable=False, default="系统")  # 操作人（后台账号名；系统动作为「系统」）
    detail = Column(Text)  # 操作详情（中文描述，便于追溯）
    created_at = Column(DateTime, default=datetime.now, index=True)  # 操作时间


class MembershipOrder(Base):
    """会员购买订单（套餐开通 / 续费的支付记录）。"""

    __tablename__ = "membership_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    order_no = Column(String(32), unique=True, index=True, nullable=False)  # 会员订单号（唯一）
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 购买用户；用户删除时级联删除
    plan = Column(String(32), nullable=False, default="monthly")  # 套餐标识，默认 monthly
    months = Column(Integer, nullable=False, default=1)  # 购买月数，默认 1
    amount = Column(Float, nullable=False, default=0)  # 支付金额（元），默认 0
    status = Column(String(16), nullable=False, default="已支付")  # 订单状态，默认「已支付」
    created_at = Column(DateTime, default=datetime.now)  # 购买时间


class CheckinRecord(Base):
    """每日签到记录（同一用户同一天仅一条，由唯一约束保证）。"""

    __tablename__ = "checkin_records"
    # 联合唯一：禁止同一用户同一天重复签到
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_checkin_user_date"),)

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 签到用户；用户删除时级联删除
    date = Column(String(10), nullable=False)  # 签到日期 `YYYY-MM-DD`
    points = Column(Integer, nullable=False, default=0)  # 本次签到获得的基础积分，默认 0
    streak = Column(Integer, nullable=False, default=0)  # 签到时的连续天数快照，默认 0
    bonus = Column(Integer, nullable=False, default=0)  # 连续签到额外奖励积分，默认 0
    created_at = Column(DateTime, default=datetime.now)  # 签到时间


class AdReward(Base):
    """广告观看奖励记录（用于每日额度统计与积分审计）。"""

    __tablename__ = "ad_rewards"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 观看用户；用户删除时级联删除
    date = Column(String(10), nullable=False)  # 奖励所属日期 `YYYY-MM-DD`，用于按天统计额度
    points = Column(Integer, nullable=False, default=0)  # 本次发放积分，默认 0
    created_at = Column(DateTime, default=datetime.now)  # 发放时间


class PointsLog(Base):
    """积分流水（每次积分变动都落一条，余额为变动后快照）。"""

    __tablename__ = "points_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 所属用户；用户删除时级联删除
    change = Column(Integer, nullable=False, default=0)  # 积分变动值：正数为获得，负数为消耗
    reason = Column(String(32), nullable=False, default="")  # 变动原因：签到 / 看广告 / 下单抵扣 / 后台调整等
    balance = Column(Integer, nullable=False, default=0)  # 变动后的积分余额快照，默认 0
    created_at = Column(DateTime, default=datetime.now)  # 变动时间


class Favorite(Base):
    """野塘收藏（用户与钓点的多对多关联表，附带收藏时间）。"""

    __tablename__ = "favorites"
    # 联合唯一：同一用户对同一钓点只保留一条收藏记录
    __table_args__ = (UniqueConstraint("user_id", "spot_id", name="uq_favorite_user_spot"),)

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 收藏用户；用户删除时级联删除
    spot_id = Column(String(16), nullable=False)  # 被收藏钓点 id（无外键，钓点删除后收藏由业务清理）
    created_at = Column(DateTime, default=datetime.now)  # 收藏时间

    # 多对一：所属用户（user.favorites 的反向引用）
    user = relationship("User", back_populates="favorites")


class UserAddress(Base):
    """用户收货地址簿（小程序端地址管理 + 支付前选择）。"""

    __tablename__ = "user_addresses"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)  # 所属用户；用户删除时级联删除
    receiver = Column(String(32), nullable=False)  # 收货人姓名
    phone = Column(String(32), nullable=False)  # 收货人手机号（11 位）
    province = Column(String(32), nullable=False)  # 省 / 直辖市
    city = Column(String(32), nullable=False)  # 地级市
    district = Column(String(64), nullable=False)  # 区县
    detail = Column(String(255), nullable=False)  # 详细地址（街道门牌等）
    # 是否默认地址：同一用户最多一条为 True（业务层保证）
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now)  # 创建时间
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)  # 最近修改时间（自动维护）


class WeatherCache(Base):
    """天气数据缓存：同一标准地址 + 同一天 3 小时内只调用一次上游天气源"""

    __tablename__ = "weather_cache"
    # 联合唯一：同一地址同一天只保留一条天气缓存
    __table_args__ = (UniqueConstraint("region", "day", name="uq_weather_region_day"),)

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键（后台批量操作以此为 id）
    # 缓存键：标准化地址 + 查询日期（同一地址同一天精准匹配）
    cache_key = Column(String(160), nullable=True, index=True)
    # 标准化地址（如「安徽省 合肥市 蜀山区」；无定位时用「默认」）
    region = Column(String(64), nullable=False, index=True)
    # 查询日期 YYYY-MM-DD
    day = Column(String(16), nullable=False)
    # 天气观测快照（weather 结构，鱼情分析单独缓存）
    payload = Column(JSON, nullable=False)
    # 数据来源：qweather / llm / fallback
    source = Column(String(16), nullable=False, default="llm")
    # 过期时间（统一 3 小时有效期）
    expire_at = Column(DateTime, nullable=True)
    # 缓存占用字节数（payload 序列化后估算）
    size_bytes = Column(Integer, nullable=False, default=0)
    # 命中次数（命中缓存返回的次数）
    hit_count = Column(Integer, nullable=False, default=0)
    # 最近一次命中时间
    last_hit_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)  # 首次写入缓存时间


class FishCache(Base):
    """鱼情分析缓存：相同查询参数 3 小时内只调用一次大模型"""

    __tablename__ = "fish_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    # 缓存键：全部查询参数规范化 JSON 的 SHA-256
    cache_key = Column(String(96), nullable=False, unique=True, index=True)
    # 标准化地址（供后台列表展示 / 搜索）
    region = Column(String(64), nullable=False, default="默认", index=True)
    # 查询参数快照（省/市/区/日期/地点等，供后台查看与排查）
    params = Column(JSON, nullable=True)
    # 大模型 / 规则引擎产出的 verdict + slots 快照
    payload = Column(JSON, nullable=False)
    # 数据来源：llm / rule
    source = Column(String(16), nullable=False, default="llm")
    # 过期时间（统一 3 小时有效期）
    expire_at = Column(DateTime, nullable=True)
    # 缓存占用字节数
    size_bytes = Column(Integer, nullable=False, default=0)
    # 命中次数
    hit_count = Column(Integer, nullable=False, default=0)
    # 最近一次命中时间
    last_hit_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)  # 首次写入缓存时间


class Admin(Base):
    """管理后台账号（与小程序用户 users 表分开）"""

    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    username = Column(String(64), unique=True, index=True, nullable=False)  # 登录名（唯一）
    password_hash = Column(String(200), nullable=False)  # 密码哈希（security.hash_password 生成）
    name = Column(String(64), nullable=False, default="系统管理员")  # 显示名称，默认「系统管理员」
    role = Column(String(32), nullable=False, default="超级管理员")  # 角色：超级管理员 / 普通管理员
    avatar_text = Column(String(8), nullable=False, default="渔")  # 头像占位文字，默认「渔」
    status = Column(String(16), nullable=False, default="normal")  # 账号状态：normal=正常，其他=停用
    created_at = Column(DateTime, default=datetime.now)  # 创建时间


class Plan(Base):
    """会员套餐（月卡 / 季卡 / 年卡，管理后台可配置）"""

    __tablename__ = "plans"

    id = Column(String(16), primary_key=True)  # 套餐 id（字符串，如 month / season / year）
    name = Column(String(32), nullable=False)  # 套餐名称
    duration = Column(Integer, nullable=False, default=30)  # 有效天数，默认 30
    duration_label = Column(String(16), nullable=False, default="30 天")  # 天数展示文案，默认「30 天」
    price = Column(Float, nullable=False, default=0)  # 现价（元），默认 0
    orig_price = Column(Float)  # 划线原价（元），可为空
    icon = Column(String(16), nullable=False, default="crown")  # 图标标识，默认 crown
    tag = Column(String(32))  # 角标文案，如「最划算」
    tag_type = Column(String(16), nullable=False, default="primary")  # 角标配色类型，默认 primary
    sales = Column(Integer, nullable=False, default=0)  # 累计销量（份），默认 0
    status = Column(String(8), nullable=False, default="off")  # 上下架：on=上架 / off=下架，默认 off
    sort = Column(Integer, nullable=False, default=0)  # 排序权重：非负整数，越大越靠前，默认 0
    benefits = Column(JSON, nullable=False, default=list)  # 权益说明列表，默认空列表
    created_at = Column(DateTime, default=datetime.now)  # 创建时间


class WeatherLog(Base):
    """天气查询日志：小程序每次查天气落一条，供管理后台查看 / 重新生成"""

    __tablename__ = "weather_logs"

    id = Column(String(16), primary_key=True)  # 业务主键（字符串短 id）
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)  # 查询用户；用户删除后置空（保留日志）
    user = Column(String(64), nullable=False, default="游客")  # 查询人昵称快照，游客为「游客」
    place = Column(String(128))  # 查询地点（如「浙江省 杭州市 西湖区」）
    place_detail = Column(String(128))  # 更精细的地点描述（手动选点 / 定位名称）
    date = Column(String(32))  # 查询日期文案
    score = Column(Integer, nullable=False, default=0)  # 鱼情评分（0-100），默认 0
    verdict = Column(String(16))  # 结论标签，如「爆护」「一般」「空军」
    level = Column(String(16))  # 鱼情等级标识
    condition = Column(String(32))  # 天气状况，如「晴」「小雨」
    temp = Column(Float)  # 气温（摄氏度）
    pressure = Column(Float)  # 气压（hPa）
    wind_diff = Column(String(32))  # 风力 / 风向差值描述
    # source：手动选点 / 手机定位
    # cost：本次查询的身份成本 —— 会员 / 普通用户（普通用户不再有广告/付费解锁，统一记为「普通用户」）
    source = Column(String(16), nullable=False, default="手动选点")
    cost = Column(String(16), nullable=False, default="普通用户")
    # 本次查询是否命中缓存（用于缓存命中率 / 命中趋势统计）
    cache_hit = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now)  # 查询时间


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
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)  # 最近修改时间（自动维护）


class OperationLog(Base):
    """管理后台操作日志。

    记录「配置变更 / 评论删除」等关键管理动作，满足审计与全链路追溯要求。
    仅追加、不删除，管理员可在「系统设置 · 操作日志」中按关键词 / 动作类型检索。
    """

    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    # 操作管理员（可能为空：进程内系统任务）
    admin_id = Column(Integer)
    admin_name = Column(String(64), nullable=False, default="系统管理员")  # 操作人昵称快照，默认「系统管理员」
    # 动作类型：config.update / comment.delete / comment.batch_delete 等，便于筛选
    action = Column(String(32), nullable=False, index=True)
    # 操作对象（如配置键、评论 id、钓点 id）
    target = Column(String(128))
    # 人类可读的变更详情（中文描述，便于审计）
    detail = Column(Text)
    # 来源 IP（尽力而为，取不到则为空）
    ip = Column(String(64))
    created_at = Column(DateTime, default=datetime.now, index=True)  # 操作时间（列表按此倒序）

