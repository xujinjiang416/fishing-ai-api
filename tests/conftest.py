"""tests/conftest.py — 测试夹具

测试策略：
- 使用 SQLite 内存库（aiosqlite + StaticPool），无需依赖本地 MySQL 即可运行；
- 通过覆盖 FastAPI 的 get_db 依赖，把业务读写全部指向测试库；
- 通过 httpx.ASGITransport 直接调用 ASGI 应用（不启动真实服务）；
- 每个测试用例前写入一份最小可用的种子数据（管理员 / 套餐 / 用户 / 钓点 / 评论 / 配置）。

执行方式：
- 在 fishing-ai-api 目录执行 `pytest`；`pytest.ini` 已设置 asyncio_mode = auto，
  因此异步用例无需逐个添加 @pytest.mark.asyncio。

隔离与清理：
- 所有夹具默认 function 作用域，每个用例独享一个内存库引擎，用例结束即销毁，
  不存在跨用例数据污染，也不使用事务回滚（直接重建库）；
- client 夹具结束时会清空依赖覆盖并清空配置服务的内存缓存，避免污染后续用例。

注意事项：
- 夹具之间存在依赖链 engine -> session_factory -> seeded -> client，
  在用例参数中声明靠后的夹具即可自动带入前置夹具；
- 若用例自行插入数据，请通过 session_factory 新建会话并 commit，不要复用 client 的请求会话。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.deps import get_db
from app.core.security import create_admin_token, create_token, hash_password
from app.main import app
from app.models import Admin, AppConfig, Plan, Spot, SpotComment, User
from app.services import config_service

# SQLite 内存库：StaticPool 让所有连接共享同一个内存数据库
TEST_DB_URL = "sqlite+aiosqlite://"


@pytest_asyncio.fixture
async def engine():
    """每个用例独立的内存数据库引擎（用例结束即销毁）。

    作用域：function。使用 StaticPool 让同一引擎的所有连接共享同一个内存库；
    建库后自动执行 Base.metadata.create_all 创建全部表，yield 之后 dispose 释放连接。
    """
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    """与测试库绑定的会话工厂。

    作用域：function（依赖 engine）。返回 async_sessionmaker：
    expire_on_commit=False 便于提交后继续读取 ORM 属性，autoflush=False 避免隐式刷新。
    用例可直接 `async with session_factory() as db:` 读写测试库。
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def seeded(session_factory):
    """写入最小可用种子数据。

    作用域：function（依赖 session_factory）。写入内容：
    - 管理员 id=1（admin / secret，超级管理员）；
    - 上架套餐 month（月卡，39.9 元 / 30 天）；
    - 普通用户 id=1（13900000001 / 123456）与会员用户 id=2（13900000002 / 123456，月卡有效）；
    - 钓点 ai_test1（杭州西湖区，2 条评论 c_1 / c_2）；
    - app_configs 的默认配置项（来自 config_service.CONFIG_DEFS）。

    说明：先清空配置服务内存缓存，确保读到本库数据；一次 commit 后直接 yield，
    不做事务回滚（隔离由每个用例独立内存库保证）。
    """
    config_service.clear_cache()
    async with session_factory() as db:
        # 管理员（id=1）
        db.add(
            Admin(
                id=1,
                username="admin",
                password_hash=hash_password("secret"),
                name="系统管理员",
                role="超级管理员",
                avatar_text="渔",
            )
        )
        # 一个上架套餐
        db.add(
            Plan(
                id="month",
                name="月卡",
                duration=30,
                duration_label="30 天",
                price=39.9,
                orig_price=59.9,
                icon="moon",
                tag="入门首选",
                tag_type="mute",
                sales=0,
                status="on",
                sort=1,
                benefits=["无限次 AI 查询", "全程无广告"],
            )
        )
        # 普通用户（id=1）与会员用户（id=2）
        normal = User(
            uid="u_norm",
            account="13900000001",
            phone="13900000001",
            nickname="普通用户",
            avatar_text="普",
            password_hash=hash_password("123456"),
            points=100,
            points_total=100,
        )
        member = User(
            uid="u_mem",
            account="13900000002",
            phone="13900000002",
            nickname="会员用户",
            avatar_text="会",
            password_hash=hash_password("123456"),
            is_member=True,
            member_expire="2027-01-01",
            plan="month",
            plan_name="月卡",
            points=200,
            points_total=200,
        )
        db.add_all([normal, member])
        await db.flush()

        # 一个钓点 + 两条评论
        db.add(
            Spot(
                id="ai_test1",
                name="测试野塘",
                region="杭州市西湖区",
                dist=1.0,
                rating=5.0,
                comment_count=2,
                fish=["鲫鱼"],
                status="online",
                gallery=[],
                added_by="系统管理员",
            )
        )
        await db.flush()
        db.add_all(
            [
                SpotComment(
                    id="c_1",
                    spot_id="ai_test1",
                    user_id=normal.id,
                    user_name="普通用户",
                    avatar="普",
                    rating=5,
                    text="鱼口很好",
                    date="2026-11-01",
                    images=[],
                ),
                SpotComment(
                    id="c_2",
                    spot_id="ai_test1",
                    user_id=member.id,
                    user_name="会员用户",
                    avatar="会",
                    rating=4,
                    text="环境不错",
                    date="2026-11-02",
                    images=[],
                ),
            ]
        )
        # 配置默认值
        for key, defn in config_service.CONFIG_DEFS.items():
            db.add(AppConfig(key=key, value=str(defn.default), remark=defn.remark))
        await db.commit()
    yield


@pytest_asyncio.fixture
async def client(session_factory, seeded):
    """基于 ASGI 的异步测试客户端（覆盖 get_db 依赖）。

    作用域：function（依赖 seeded，确保库内已有基础数据）。
    依赖注入方式：把 app 的 get_db 覆盖为测试会话工厂，业务代码无感知；
    清理逻辑：yield 结束后移除依赖覆盖并清空配置缓存，避免影响其它测试文件。
    """

    async def _override_get_db():
        """测试用数据库依赖：每个请求创建独立会话，响应结束后自动关闭。"""
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    config_service.clear_cache()


@pytest.fixture
def admin_headers():
    """管理员鉴权头（admin id=1）。

    作用域：function（同步夹具）。返回形如 `{"Authorization": "Bearer <token>"}` 的字典，
    token 由 create_admin_token(1) 现场签发，与 seeded 中的管理员 id 对应。
    """
    return {"Authorization": "Bearer " + create_admin_token(1)}


@pytest.fixture
def normal_headers():
    """普通用户鉴权头（user id=1）。

    作用域：function（同步夹具）。对应 seeded 中的普通用户（非会员）。
    """
    return {"Authorization": "Bearer " + create_token(1)}


@pytest.fixture
def member_headers():
    """会员用户鉴权头（user id=2）。

    作用域：function（同步夹具）。对应 seeded 中的会员用户（月卡有效）。
    """
    return {"Authorization": "Bearer " + create_token(2)}
