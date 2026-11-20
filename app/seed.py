"""seed.py — 幂等初始化数据

职责：
- 建表（create_all）并为已存在的旧库补齐新增列（_ensure_columns）；
- 清理早期写死的种子钓点（_purge_legacy_spots）；
- 灌入省市县 / 商品 / 管理员 / 会员套餐 / 积分配置等基础数据；
- 不再创建演示账号与演示业务数据，避免清理后被启动流程重新写回。

执行方式：
- 命令行：`python -m app.seed`（脚本入口在文件末尾）；
- 也可在服务启动时调用 `run_seed()`（见 main.py），传入已有会话可复用同一事务。

前置条件：
- 数据库连接由 `app.core.database` 的 engine / SessionLocal 决定（默认 MySQL，可通过 .env 覆盖）；
- 管理员账号的用户名 / 密码取自 `settings`（admin_username / admin_password）。

注意事项：
- 所有写入均以「表为空才插入」或「按唯一键查重后补齐」的方式实现，可反复执行；
- 种子数据仅用于开发 / 演示，生产环境应通过后台维护真实数据；
- 修改种子内容不会覆盖已存在记录，需手工清理对应表后重跑。
"""

import asyncio

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .core.config import settings
from .core.database import Base, SessionLocal, engine
from .core.security import hash_password
from .models import Admin, AppConfig, City, District, Goods, Plan, Province
from .seed_data import GOODS, PLANS, PROVINCES
from .services import config_service


async def _count(db: AsyncSession, model) -> int:
    """统计指定表的行数，作为「是否已灌入种子」的幂等判据。

    Args:
        db: 异步数据库会话。
        model: 目标 ORM 模型类。

    Returns:
        表内行数；查询结果为 NULL 时返回 0。
    """
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def _seed_geo(db: AsyncSession) -> None:
    """写入省 / 市 / 区县三级行政区划（仅当 provinces 表为空时执行）。

    数据源为 `seed_data.PROVINCES`，层级结构为「省 -> 市 -> 区县」；
    `sort` 取列表下标，保证前端下拉按原始顺序展示。整体一次提交，避免半成品数据。
    """
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
    """写入商品种子（仅当 goods 表为空时执行）。

    字段取自 `seed_data.GOODS`：id 为固定业务编号（如 g1），价格 / 划线价单位元，
    tags 为展示标签列表，stock 为库存，缺失的可选字段用 get 兜底（tags 为空列表）。
    """
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


async def _seed_admin(db: AsyncSession) -> None:
    """内置管理后台账号（admins 表，与小程序 users 分表）。

    幂等策略：以 username == settings.admin_username 查重；已存在时仅补齐缺失的密码哈希。
    默认账号：settings.admin_username / settings.admin_password，角色为「超级管理员」，状态 normal。
    """
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
    """写入会员套餐种子（仅当 plans 表为空时执行）。

    数据源为 `seed_data.PLANS`：duration 为有效天数，price / orig 为现价与原价（元），
    tag_type 控制前端标签配色，benefits 为权益文案列表；sales 初始为 0，status 固定上架（on）。
    """
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


async def run_seed(db: AsyncSession | None = None) -> None:
    """初始化入口：先补齐表结构，再按需灌入全部种子数据。

    Args:
        db: 可选的异步会话。传入时直接复用（由调用方管理提交与关闭）；
            不传则内部创建 `SessionLocal` 会话。

    说明：建表 / 补列 / 清理旧钓点通过 engine.begin() 的同步连接执行，
    因此无论是否传入会话都会生效；seed 数据写入不具备跨步骤事务性，
    各子步骤内部自行 commit，便于失败后重跑。
    """
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
    """给已存在的库补上新增列（create_all 不会修改已有表）。

    逐表比对 information_schema 中的列集合，缺失时执行 ALTER TABLE ADD COLUMN；
    对带 NOT NULL 默认值的新列，额外回填历史行的 NULL 值。
    仅做「加列」不做改列 / 删列，重复执行时所有分支都会跳过，幂等安全。
    """
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
    """按依赖顺序依次执行各数据集种子写入。

    顺序要求：行政区划 / 商品 / 管理员先落库，套餐与配置相互独立；
    每步内部自带幂等判断。演示账号与演示业务数据已停止写入。
    """
    await _seed_geo(db)
    await _seed_goods(db)
    await _seed_admin(db)
    await _seed_plans(db)
    await _seed_configs(db)


if __name__ == "__main__":
    asyncio.run(run_seed())
    print("初始化完成：省市县 / 商品 / 管理员 / 会员套餐 / 积分配置")
