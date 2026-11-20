"""auth.py — 登录 / 注册 / 退出

职责：小程序端账号体系入口，覆盖手机号密码注册 / 登录、微信小程序 code 登录、
退出登录与演示账号一键登录；成功后统一签发 JWT 并返回用户信息。

鉴权要求：本文件所有接口均为公开接口（不校验登录态）；仅 /demo 依赖已初始化的演示数据。

依赖服务：
- MySQL（users 表，AsyncSession 会话）；
- core.security：JWT 签发（create_token）与密码哈希 / 校验（hash_password / verify_password）；
- services.wechat：微信 code2session（未配置 appid 时返回本地模拟 openid）。

注意事项：
- JWT 无状态，服务端不保存会话，退出登录由前端丢弃 token 即可；
- 登录失败统一返回同一条文案，避免通过错误差异枚举已注册手机号；
- 密码只保存哈希，任何响应都不会回传 password_hash。
"""

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

# 演示账号在 users.account 字段中的固定标识；由 `python -m app.seed` 初始化。
# 仅用于本地调试 / 免注册体验，生产环境应移除该路由或限制访问。
DEMO_ACCOUNT = "demo"


def _new_user(
    openid: str | None = None,
    nickname: str | None = None,
    phone: str | None = None,
    password_hash: str | None = None,
) -> User:
    """构造新用户实体（只建对象，不落库，由调用方 add / commit）。

    参数：openid 微信身份标识；nickname 昵称；phone 手机号；
    password_hash 已哈希的密码（明文不落库）。
    生成规则：uid 取 `u_` + 8 位随机十六进制（唯一索引由数据库兜底）；
    昵称缺省为「钓友 + 手机号 / openid 末 4 位」；头像文字取昵称首字；
    初始等级固定 Lv.1 新手，注册日期取当天。
    """
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
    """注册新用户（手机号即账号）。

    方法与路径：POST /auth/register（公开）。
    请求参数 RegisterIn：phone 必须为 11 位手机号；password 6-16 位（均由 schema 校验）；
    confirmPassword 可选，传入时必须与 password 一致；nickname 可选。
    响应：{ token, user, isNew: true } —— 注册成功即签发登录态，前端无需再登录。
    限制：同一手机号只能注册一次，重复注册返回 400；密码仅保存哈希。
    """
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
    """手机号 + 密码登录。

    方法与路径：POST /auth/login（公开）。
    请求参数 PasswordLoginIn：account 必须是 11 位手机号；password 6-16 位（schema 校验）。
    响应：{ token, user, isNew: false }，token 为 30 天有效期的 JWT。
    鉴权说明：账号不存在与密码错误返回同一条提示，防止账号枚举。
    """
    user = await db.scalar(select(User).where(User.phone == payload.account))
    if not user or not verify_password(payload.password, user.password_hash):
        # 不区分「账号不存在」与「密码错误」，避免账号枚举
        raise HTTPException(status_code=400, detail="手机号或密码不正确")
    return ok({"token": create_token(user.id), "user": user_out(user), "isNew": False})


@router.post("/wechat", summary="微信小程序登录（code 换登录态）")
async def wechat_login(payload: WechatLoginIn, db: AsyncSession = Depends(get_db)):
    """微信小程序登录：用 wx.login 的 code 换取 openid。

    方法与路径：POST /auth/wechat（公开）。
    请求参数 WechatLoginIn：code 微信登录临时凭证；nickname 可选昵称。
    响应：{ token, user }。
    第三方调用：services.wechat.code2session —— 未配置 WECHAT_APPID 时返回由 code
    稳定映射的模拟 openid（便于本地联调）；配置后需补全真实 jscode2session 调用。
    逻辑：openid 已存在则直接登录，否则自动注册（昵称缺省按规则生成）。
    """
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
    """退出登录。

    方法与路径：POST /auth/logout（公开）。
    JWT 无状态，服务端不维护会话：退出由前端丢弃 token 即可；此接口保留给后续接入
    token 黑名单，当前无任何副作用，始终返回成功。
    """
    return ok(None, "已退出登录")


@router.get("/demo", summary="一键进入演示账号（调试用）")
async def demo(db: AsyncSession = Depends(get_db)):
    """一键登录演示账号（调试用）。

    方法与路径：GET /auth/demo（公开）。
    响应：{ token, user }。
    限制：演示账号需先执行 `python -m app.seed` 初始化，未初始化返回 404；
    本接口签发真实 token，生产环境应关闭或加访问控制。
    """
    user = await db.scalar(select(User).where(User.account == DEMO_ACCOUNT))
    if not user:
        raise HTTPException(status_code=404, detail="演示账号未初始化，请先执行 python -m app.seed")
    return ok({"token": create_token(user.id), "user": user_out(user)})
