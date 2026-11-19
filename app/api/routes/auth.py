"""auth.py — 登录 / 注册 / 退出"""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_db
from ...core.response import ok
from ...core.security import create_token, hash_password, verify_password
from ...core.utils import today
from ...models import User
from ...schemas.schemas import PasswordLoginIn, RegisterIn, WechatLoginIn, user_out
from ...services import wechat

router = APIRouter(prefix="/auth", tags=["鉴权"])

DEMO_ACCOUNT = "demo"


def _new_user(
    openid: str | None = None,
    nickname: str | None = None,
    phone: str | None = None,
    password_hash: str | None = None,
) -> User:
    seed_name = phone or openid or "钓友"
    return User(
        uid="u_" + uuid4().hex[:8],
        openid=openid,
        phone=phone,
        password_hash=password_hash,
        nickname=nickname or f"钓友{seed_name[-4:]}",
        avatar_text=(nickname or seed_name)[:1],
        level="Lv.1 新手",
        joined=today(),
        location=None,
    )


@router.post("/register", summary="注册（手机号 + 密码 6-16 位）")
async def register(payload: RegisterIn, db: AsyncSession = Depends(get_db)):
    """账号即手机号；手机号已注册则提示直接登录。"""
    phone = payload.phone  # 已在 schema 中校验为 11 位手机号
    if await db.scalar(select(User).where(User.phone == phone)):
        raise HTTPException(status_code=400, detail="该手机号已注册，请直接登录")

    user = _new_user(
        phone=phone,
        nickname=payload.nickname,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return ok({"token": create_token(user.id), "user": user_out(user), "isNew": True})


@router.post("/login", summary="登录（手机号 + 密码）")
async def login(payload: PasswordLoginIn, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.phone == payload.account))
    if not user or not verify_password(payload.password, user.password_hash):
        # 不区分「账号不存在」与「密码错误」，避免账号枚举
        raise HTTPException(status_code=400, detail="手机号或密码不正确")
    return ok({"token": create_token(user.id), "user": user_out(user), "isNew": False})


@router.post("/wechat", summary="微信小程序登录（code 换登录态）")
async def wechat_login(payload: WechatLoginIn, db: AsyncSession = Depends(get_db)):
    session = wechat.code2session(payload.code)
    openid = session["openid"]

    user = await db.scalar(select(User).where(User.openid == openid))
    if not user:
        user = _new_user(openid=openid, nickname=payload.nickname)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return ok({"token": create_token(user.id), "user": user_out(user)})


@router.post("/logout", summary="退出登录")
async def logout():
    """JWT 无状态，退出由前端丢弃 token 即可；此接口保留给后续接入黑名单。"""
    return ok(None, "已退出登录")


@router.get("/demo", summary="一键进入演示账号（调试用）")
async def demo(db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.account == DEMO_ACCOUNT))
    if not user:
        raise HTTPException(status_code=404, detail="演示账号未初始化，请先执行 python -m app.seed")
    return ok({"token": create_token(user.id), "user": user_out(user)})
