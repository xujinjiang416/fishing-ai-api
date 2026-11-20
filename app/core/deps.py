"""deps.py — FastAPI 依赖注入（数据库会话 / 当前登录用户）

职责：
    1. get_db：按请求创建并自动关闭 AsyncSession；
    2. 从 `Authorization: Bearer <token>` 解析出小程序用户 id 或后台管理员 id；
    3. 提供「必须登录 / 游客可选 / 后台管理员 / 超级管理员」四类鉴权依赖。

关键流程：
    请求头 → 拆出 Bearer token → security.parse_token / parse_admin_token 校验签名与有效期
    → 返回 user_id / admin_id，必要时查库校验后台账号状态与角色。

注意事项：
    - 小程序 token 与后台 token 共用 JWT 密钥，但后台 token 额外带 typ=admin 做隔离；
    - 401 表示未登录/过期（前端跳登录页），403 表示已登录但权限不足。
"""

from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Admin
from .database import SessionLocal
from .security import parse_admin_token, parse_token


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """数据库会话依赖：请求结束后自动关闭 Session。

    Yields:
        AsyncSession: 绑定全局引擎的异步会话，业务内可直接 await 执行 ORM 操作。
    """
    # async with 保证异常路径下也会 close，避免连接泄漏
    async with SessionLocal() as session:
        yield session


def _user_id_from_header(authorization: str | None) -> int | None:
    """从请求头解析小程序用户 id（内部工具函数，不抛异常）。

    Args:
        authorization: 原始 Authorization 头，形如 `Bearer <token>`。

    Returns:
        int | None: 解析成功返回 user_id；缺失/格式不对/token 无效均返回 None。
    """
    if not authorization:
        return None
    # 必须是「Bearer <token>」两段结构，大小写不敏感
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parse_token(parts[1])


def _admin_id_from_header(authorization: str | None) -> int | None:
    """从请求头解析管理后台账号 id（内部工具函数，不抛异常）。

    Args:
        authorization: 原始 Authorization 头，形如 `Bearer <token>`。

    Returns:
        int | None: 解析成功返回 admin_id；缺失/格式不对/token 非后台类型均返回 None。
    """
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parse_admin_token(parts[1])


def get_current_user_id(authorization: str | None = Header(default=None)) -> int:
    """需要登录的接口使用；未登录直接 401，前端据此跳登录页。

    Args:
        authorization: 请求头 Authorization（FastAPI 自动从 Header 注入）。

    Returns:
        int: 当前登录用户 id。

    Raises:
        HTTPException: 401，未携带 token、token 格式错误或已过期。
    """
    user_id = _user_id_from_header(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user_id


def get_optional_user_id(authorization: str | None = Header(default=None)) -> int | None:
    """游客也可访问、但登录后结果不同的接口使用（如天气、野塘列表）。

    Args:
        authorization: 请求头 Authorization。

    Returns:
        int | None: 已登录返回用户 id，游客返回 None（不抛异常）。
    """
    return _user_id_from_header(authorization)


async def get_current_admin(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    """管理后台接口使用：校验 admin token 并从 admins 表取账号。

    Args:
        authorization: 请求头 Authorization（后台 token 带 typ=admin）。
        db: 数据库会话（依赖注入）。

    Returns:
        Admin: 状态为 normal 的管理员 ORM 对象。

    Raises:
        HTTPException: 401，token 缺失/无效、账号不存在或已被停用。
    """
    admin_id = _admin_id_from_header(authorization)
    if admin_id is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    # token 有效只代表签名正确，仍需校验账号是否被删除/停用
    admin = await db.get(Admin, admin_id)
    if not admin:
        raise HTTPException(status_code=401, detail="账号不存在")
    if admin.status != "normal":
        raise HTTPException(status_code=401, detail="账号已被停用")
    return admin


async def get_super_admin(admin: Admin = Depends(get_current_admin)) -> Admin:
    """仅超级管理员可用的接口（缓存刷新/删除/规则修改等高危操作）。

    Args:
        admin: 已通过登录校验的管理员（依赖注入）。

    Returns:
        Admin: 角色为「超级管理员」的账号。

    Raises:
        HTTPException: 403，当前管理员角色不具备该操作权限。
    """
    if admin.role != "超级管理员":
        raise HTTPException(status_code=403, detail="无权限执行该操作")
    return admin


# 路由层常用的依赖别名，简化函数签名书写（用法：user_id: int = CurrentUserId）
CurrentUserId = Depends(get_current_user_id)      # 强制登录
OptionalUserId = Depends(get_optional_user_id)    # 游客可选
DbSession = Depends(get_db)                       # 数据库会话
