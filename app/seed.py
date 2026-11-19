"""seed.py — 幂等初始化数据

执行：python -m app.seed
也可以在服务启动时自动调用（见 main.py）。
"""

import asyncio
from datetime import date, datetime, time, timedelta

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .core.config import settings
from .core.database import Base, SessionLocal, engine
from .core.security import hash_password
from .core.utils import today
from .models import (
    Admin,
    AppConfig,
    City,
    District,
    Goods,
    Order,
    OrderLog,
    Plan,
    Province,
    User,
    WeatherLog,
)
from .seed_data import GOODS, PLANS, PROVINCES
from .services import ai, config_service

DEMO_ACCOUNT = "demo"


async def _count(db: AsyncSession, model) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def _seed_geo(db: AsyncSession) -> None:
    if await _count(db, Province):
        return
    for pi, p in enumerate(PROVINCES):
        province = Province(name=p["name"], sort=pi)
        db.add(province)
        await db.flush()
        for ci, c in enumerate(p["cities"]):
            city = City(province_id=province.id, name=c["name"], sort=ci)
            db.add(city)
            await db.flush()
            for di, d in enumerate(c["districts"]):
                db.add(District(city_id=city.id, name=d, sort=di))
    await db.commit()


async def _seed_goods(db: AsyncSession) -> None:
    if await _count(db, Goods):
        return
    for g in GOODS:
        db.add(
            Goods(
                id=g["id"],
                name=g["name"],
                cat=g["cat"],
                price=g["price"],
                orig=g.get("orig"),
                img=g.get("img"),
                sales=g.get("sales"),
                tags=g.get("tags") or [],
                spec=g.get("spec"),
                stock=g.get("stock", 0),
                desc=g.get("desc"),
            )
        )
    await db.commit()


async def _seed_demo_user(db: AsyncSession) -> None:
    """内置演示账号：用「演示手机号 + 演示密码」直接登录，数据与原型保持一致。"""
    demo_hash = hash_password(settings.demo_password)
    existing = await db.scalar(select(User).where(User.account == DEMO_ACCOUNT))
    if existing:
        # 保证演示账号可直接用手机号 + 密码登录
        if existing.phone != settings.demo_phone or not existing.password_hash:
            existing.phone = settings.demo_phone
            existing.password_hash = demo_hash
            await db.commit()
        return

    user = User(
        uid="u_88213",
        account=DEMO_ACCOUNT,
        nickname="钓王老张",
        avatar_text="张",
        phone=settings.demo_phone,
        password_hash=demo_hash,
        level="Lv.4 老钓友",
        city="杭州",
        joined="2025-08-12",
        is_member=False,
        member_expire="2026-12-05",
        points=990,
        points_total=1860,
        streak=4,
        max_streak=12,
        last_checkin_date="",
        ads_date=today(),
        ads_watched_today=3,
        ad_points_today=15,
        location={"province": "浙江省", "city": "杭州市", "district": "西湖区", "source": "manual"},
    )
    db.add(user)
    await db.flush()

    # 演示订单：覆盖待支付 / 待发货 / 已发货 / 已完成 四种状态，便于后台订单管理开箱可用
    today_date = date.today()
    demo_orders = [
        {
            "order_no": "F20261103008",
            "goods_id": "g3",
            "goods_name": "五谷诱综合钓饵 500g",
            "qty": 1,
            "total": 19.9,
            "use_points": 120,
            "pay_amount": 19.1,
            "status": "待发货",
            "pay_method": "微信支付",
            "delivery_type": "快递",
            "receiver": "张先生",
            "receiver_phone": settings.demo_phone,
            "address": "浙江省杭州市西湖区王家村 3 组 12 号",
            "days_ago": 1,
        },
        {
            "order_no": "F20261029021",
            "goods_id": "g3",
            "goods_name": "五谷诱钓饵料 500g",
            "qty": 1,
            "total": 12.9,
            "use_points": 0,
            "pay_amount": 12.9,
            "status": "已完成",
            "pay_method": "微信支付",
            "delivery_type": "快递",
            "receiver": "张先生",
            "receiver_phone": settings.demo_phone,
            "address": "浙江省杭州市西湖区王家村 3 组 12 号",
            "logistics_company": "顺丰速运",
            "logistics_no": "SF1234567890123",
            "days_ago": 6,
        },
        {
            "order_no": "F20261115003",
            "goods_id": "g1",
            "goods_name": "碳素轻量台钓竿 4.5m",
            "qty": 1,
            "total": 199.0,
            "use_points": 1500,
            "pay_amount": 189.0,
            "status": "待支付",
            "pay_method": "微信支付",
            "delivery_type": "快递",
            "receiver": "张先生",
            "receiver_phone": settings.demo_phone,
            "address": "浙江省杭州市西湖区王家村 3 组 12 号",
            "days_ago": 0,
        },
        {
            "order_no": "F20261112009",
            "goods_id": "g6",
            "goods_name": "强光夜钓灯 充电式",
            "qty": 1,
            "total": 79.0,
            "use_points": 0,
            "pay_amount": 79.0,
            "status": "已发货",
            "pay_method": "微信支付",
            "delivery_type": "快递",
            "receiver": "张先生",
            "receiver_phone": settings.demo_phone,
            "address": "浙江省杭州市西湖区王家村 3 组 12 号",
            "logistics_company": "中通快递",
            "logistics_no": "ZT9876543210987",
            "days_ago": 3,
        },
    ]
    for o in demo_orders:
        created = datetime.combine(today_date - timedelta(days=o.pop("days_ago")), time(10, 30))
        order = Order(
            user_id=user.id,
            paid_at=None if o["status"] == "待支付" else created,
            shipped_at=created if o["status"] in ("已发货", "已完成") else None,
            completed_at=created if o["status"] == "已完成" else None,
            created_at=created,
            **o,
        )
        db.add(order)
        db.add(
            OrderLog(
                order_no=o["order_no"],
                action="create",
                operator=user.nickname,
                detail=f"用户下单：{o['goods_name']} × {o['qty']}，实付 ¥{o['pay_amount']}",
                created_at=created,
            )
        )
    await db.commit()


async def _seed_admin(db: AsyncSession) -> None:
    """内置管理后台账号（admins 表，与小程序 users 分表）。"""
    admin_hash = hash_password(settings.admin_password)
    existing = await db.scalar(select(Admin).where(Admin.username == settings.admin_username))
    if existing:
        if not existing.password_hash:
            existing.password_hash = admin_hash
            await db.commit()
        return
    db.add(
        Admin(
            username=settings.admin_username,
            password_hash=admin_hash,
            name="系统管理员",
            role="超级管理员",
            avatar_text="渔",
            status="normal",
        )
    )
    await db.commit()


async def _seed_plans(db: AsyncSession) -> None:
    if await _count(db, Plan):
        return
    for p in PLANS:
        db.add(
            Plan(
                id=p["id"],
                name=p["name"],
                duration=p["duration"],
                duration_label=f"{p['duration']} 天",
                price=p["price"],
                orig_price=p["orig"],
                icon=p["icon"],
                tag=p["tag"],
                tag_type=p["tag_type"],
                sales=0,
                status="on",
                sort=p["sort"],
                benefits=p["benefits"],
            )
        )
    await db.commit()


# 演示天气日志样本：分数/天气/地点铺满近 7 天，保证看板趋势与列表首屏有数据
_SEED_LOGS = [
    ("86", "多云转晴", 17.0, 1018.6, "王家村后塘 · 3 公里范围内"),
    ("74", "晴", 19.0, 1016.2, "钱塘江支流 · 3 公里范围内"),
    ("61", "阴", 16.0, 1013.8, "西溪湿地北门 · 3 公里范围内"),
    ("45", "小雨", 14.0, 1010.5, "余杭塘河 · 3 公里范围内"),
    ("90", "多云", 21.0, 1019.4, "王家村后塘 · 3 公里范围内"),
    ("68", "晴", 18.0, 1017.1, "贴沙河 · 3 公里范围内"),
    ("52", "阴转多云", 15.0, 1012.3, "铜鉴湖 · 3 公里范围内"),
    ("79", "多云", 20.0, 1018.0, "钱塘江支流 · 3 公里范围内"),
]


async def _seed_weather_logs(db: AsyncSession) -> None:
    if await _count(db, WeatherLog):
        return
    demo = await db.scalar(select(User).where(User.account == DEMO_ACCOUNT))
    if not demo:
        return

    today_date = date.today()
    for i, (score, cond, temp, pressure, detail) in enumerate(_SEED_LOGS):
        d = today_date - timedelta(days=len(_SEED_LOGS) - 1 - i)
        v = ai.verdict_of(float(score))
        db.add(
            WeatherLog(
                id=f"wx_10{40 + i:03d}",
                user_id=demo.id,
                user=demo.nickname,
                place="浙江省 杭州市 西湖区",
                place_detail=detail,
                date=f"{d.isoformat()} 0{6 + i % 4}:{(10 + i * 7) % 60:02d}",
                score=int(score),
                verdict=v["name"],
                level=v["level"],
                condition=cond,
                temp=temp,
                pressure=pressure,
                wind_diff="东北风 2 级" if i % 2 == 0 else "东南风 3 级",
                source="手动选点" if i % 3 else "手机定位",
                cost=["广告解锁", "会员", "¥0.99 单次"][i % 3],
                created_at=datetime.combine(d, time(6 + i % 2 * 8, (10 + i * 7) % 60)),
            )
        )
    await db.commit()


async def run_seed(db: AsyncSession | None = None) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)
        await conn.run_sync(_purge_legacy_spots)

    if db is not None:
        await _seed_all(db)
        return

    async with SessionLocal() as session:
        await _seed_all(session)


def _ensure_columns(sync_conn) -> None:
    """给已存在的库补上新增列（create_all 不会修改已有表）。"""
    insp = sa.inspect(sync_conn)

    cols = {c["name"] for c in insp.get_columns("users")}
    if "password_hash" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(200)"))
    if "plan" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE users ADD COLUMN plan VARCHAR(16) NOT NULL DEFAULT 'none'"))
    if "plan_name" not in cols:
        sync_conn.execute(
            sa.text("ALTER TABLE users ADD COLUMN plan_name VARCHAR(32) NOT NULL DEFAULT '未开通'")
        )
    if "plan_expire" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE users ADD COLUMN plan_expire VARCHAR(16)"))
    if "status" not in cols:
        sync_conn.execute(
            sa.text("ALTER TABLE users ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'normal'")
        )
    if "last_active" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE users ADD COLUMN last_active VARCHAR(32)"))

    cols = {c["name"] for c in insp.get_columns("goods")}
    if "points_enabled" not in cols:
        sync_conn.execute(
            sa.text("ALTER TABLE goods ADD COLUMN points_enabled TINYINT(1) NOT NULL DEFAULT 1")
        )
    if "status" not in cols:
        sync_conn.execute(
            sa.text("ALTER TABLE goods ADD COLUMN status VARCHAR(8) NOT NULL DEFAULT 'on'")
        )
        sync_conn.execute(sa.text("UPDATE goods SET status='on' WHERE status IS NULL"))
    if "updated" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE goods ADD COLUMN updated VARCHAR(16)"))

    cols = {c["name"] for c in insp.get_columns("spots")}
    if "region" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE spots ADD COLUMN region VARCHAR(32)"))
    if "refreshed_at" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE spots ADD COLUMN refreshed_at DATETIME"))
    if "status" not in cols:
        sync_conn.execute(
            sa.text("ALTER TABLE spots ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'online'")
        )
        sync_conn.execute(sa.text("UPDATE spots SET status='online' WHERE status IS NULL"))
    if "added_by" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE spots ADD COLUMN added_by VARCHAR(64)"))
    if "created_at" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE spots ADD COLUMN created_at DATETIME"))
        sync_conn.execute(
            sa.text("UPDATE spots SET created_at=refreshed_at WHERE created_at IS NULL")
        )

    # 订单管理扩展字段（支付 / 配送 / 收货 / 备注 / 退款 / 状态时间戳）
    cols = {c["name"] for c in insp.get_columns("orders")}
    order_columns = {
        "pay_method": "VARCHAR(16) NOT NULL DEFAULT '微信支付'",
        "delivery_type": "VARCHAR(16) NOT NULL DEFAULT '快递'",
        "receiver": "VARCHAR(32)",
        "receiver_phone": "VARCHAR(32)",
        "address": "VARCHAR(255)",
        "logistics_company": "VARCHAR(32)",
        "logistics_no": "VARCHAR(64)",
        "remark": "TEXT",
        "abnormal": "TINYINT(1) NOT NULL DEFAULT 0",
        "refund_amount": "FLOAT NOT NULL DEFAULT 0",
        "refund_status": "VARCHAR(16) NOT NULL DEFAULT 'none'",
        "refund_reason": "TEXT",
        "paid_at": "DATETIME",
        "shipped_at": "DATETIME",
        "completed_at": "DATETIME",
        "closed_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    for name, ddl in order_columns.items():
        if name not in cols:
            sync_conn.execute(sa.text(f"ALTER TABLE orders ADD COLUMN {name} {ddl}"))

    # 天气缓存键（标准化地址 + 日期）
    cols = {c["name"] for c in insp.get_columns("weather_cache")}
    if "cache_key" not in cols:
        sync_conn.execute(sa.text("ALTER TABLE weather_cache ADD COLUMN cache_key VARCHAR(160)"))

    # 鱼情缓存标准化地址（供后台搜索）
    cols = {c["name"] for c in insp.get_columns("fish_cache")}
    if "region" not in cols:
        sync_conn.execute(
            sa.text("ALTER TABLE fish_cache ADD COLUMN region VARCHAR(64) NOT NULL DEFAULT '默认'")
        )


def _purge_legacy_spots(sync_conn) -> None:
    """清掉早期写死的种子野塘（id 形如 s1），只保留大模型生成的钓点（id 以 ai 开头）。

    连带清空它们的评论与收藏，避免留下悬空数据。幂等，可在每次启动时执行。
    """
    sync_conn.execute(sa.text("DELETE FROM favorites WHERE spot_id NOT LIKE 'ai%'"))
    sync_conn.execute(sa.text("DELETE FROM spot_comments WHERE spot_id NOT LIKE 'ai%'"))
    sync_conn.execute(sa.text("DELETE FROM spots WHERE id NOT LIKE 'ai%'"))


async def _seed_configs(db: AsyncSession) -> None:
    """写入积分配置的默认值（表为空时）。

    目的是让后台「积分配置」页面开箱即有可见、可编辑的配置项；
    即使此处未写入，业务读取时也会用 CONFIG_DEFS 的默认值兜底。
    """
    if await _count(db, AppConfig):
        return
    for key, defn in config_service.CONFIG_DEFS.items():
        db.add(AppConfig(key=key, value=str(defn.default), remark=defn.remark))
    await db.commit()


async def _seed_all(db: AsyncSession) -> None:
    await _seed_geo(db)
    await _seed_goods(db)
    await _seed_demo_user(db)
    await _seed_admin(db)
    await _seed_plans(db)
    await _seed_weather_logs(db)
    await _seed_configs(db)


if __name__ == "__main__":
    asyncio.run(run_seed())
    print("初始化完成：省市县 / 商品 / 演示账号 / 管理员 / 会员套餐 / 天气日志 / 积分配置")
