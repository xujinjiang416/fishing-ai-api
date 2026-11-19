"""deps.py — FastAPI 依赖注入（数据库会话 / 当前登录用户）"""

from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Admin
from .database import SessionLocal
from .security import parse_admin_token, parse_token


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


def _user_id_from_header(authorization: str | None) -> int | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parse_token(parts[1])


def _admin_id_from_header(authorization: str | None) -> int | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parse_admin_token(parts[1])


def get_current_user_id(authorization: str | None = Header(default=None)) -> int:
    """需要登录的接口使用；未登录直接 401，前端据此跳登录页。"""
    user_id = _user_id_from_header(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user_id


def get_optional_user_id(authorization: str | None = Header(default=None)) -> int | None:
    """游客也可访问、但登录后结果不同的接口使用（如天气、野塘列表）。"""
    return _user_id_from_header(authorization)


async def get_current_admin(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    """管理后台接口使用：校验 admin token 并从 admins 表取账号。"""
    admin_id = _admin_id_from_header(authorization)
    if admin_id is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    admin = await db.get(Admin, admin_id)
    if not admin:
        raise HTTPException(status_code=401, detail="账号不存在")
    if admin.status != "normal":
        raise HTTPException(status_code=401, detail="账号已被停用")
    return admin


async def get_super_admin(admin: Admin = Depends(get_current_admin)) -> Admin:
    """仅超级管理员可用的接口（缓存刷新/删除/规则修改等高危操作）。"""
    if admin.role != "超级管理员":
        raise HTTPException(status_code=403, detail="无权限执行该操作")
    return admin


CurrentUserId = Depends(get_current_user_id)
OptionalUserId = Depends(get_optional_user_id)
DbSession = Depends(get_db)
