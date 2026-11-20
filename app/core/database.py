"""database.py — SQLAlchemy 异步引擎 / 会话 / 基类

职责：
    1. 依据 settings.database_url 创建全局异步引擎（连接池）；
    2. 提供会话工厂 SessionLocal，供 FastAPI 依赖 get_db 按请求创建 Session；
    3. 导出声明式基类 Base，所有 ORM 模型继承它并映射到同名数据表。

关键流程：
    create_async_engine → async_sessionmaker → 路由中 `async with SessionLocal()` 使用。

依赖：
    - SQLAlchemy 2.x 异步扩展（sqlalchemy.ext.asyncio）；
    - 数据库驱动：aiomysql（异步 MySQL，需在 .env 中配置）。

注意事项：
    - 路由内所有 DB 操作都必须 await，禁止同步阻塞调用；
    - session 由依赖注入统一关闭，业务代码不要手动 commit 之外的资源管理。
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

# 全局异步引擎：进程内共享连接池，禁止在请求中重复创建
engine = create_async_engine(
    settings.database_url,  # 数据库连接串（见 Settings.database_url）
    pool_pre_ping=True,     # 取连接前先 ping，自动剔除被 MySQL 断开的失效连接
    pool_recycle=3600,      # 连接最长复用 3600 秒，避免超过 MySQL wait_timeout
    echo=False,             # 是否打印 SQL；调试时可临时改为 True
)

# 会话工厂：每个请求创建一个 AsyncSession
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,        # 关闭自动 flush，写操作由业务显式控制时机
    expire_on_commit=False, # commit 后对象属性仍可用（避免响应序列化时触发额外查询）
)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类（提供 metadata 与表映射能力）。"""

    pass
