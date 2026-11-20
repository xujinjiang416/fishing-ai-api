"""security.py — JWT 签发与校验 + 密码哈希

职责：
    1. 用户密码使用 PBKDF2-SHA256 + 随机盐做哈希存储，校验时用常量时间比较；
    2. 签发 / 解析小程序用户 token（create_token / parse_token）；
    3. 签发 / 解析管理后台 token（create_admin_token / parse_admin_token，额外带 typ=admin）。

关键流程：
    小程序侧登录态收敛为一个 Bearer Token：账号密码登录（或微信登录）换 token，
    后续所有需要身份的接口都带 `Authorization: Bearer <token>`。

依赖：
    - PyJWT：token 编解码；密钥与算法来自 core.config.settings；
    - hashlib / hmac / os：标准库实现的密码哈希，未引入 passlib。

注意事项：
    - 用户 token 与后台 token 共用同一签名密钥，靠 payload.typ 区分用途，避免互相冒用；
    - 本模块为同步函数（CPU 计算量小），无需 await。
"""

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

# 密码哈希算法标识（写入哈希串首位，便于将来算法升级时共存）
_ALGO = "pbkdf2_sha256"
# PBKDF2 迭代次数：越大越安全但越慢，120000 为当前折中值
_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    """生成密码哈希（pbkdf2-sha256 + 随机盐），格式：algo$iterations$salt$hash

    Args:
        password: 明文密码（长度由上层校验，如 6-16 位）。

    Returns:
        str: 拼接后的哈希串，可安全落库（salt 与 hash 均为 base64）。
    """
    # 每次哈希生成 16 字节随机盐，保证相同密码得到不同结果
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join([
        _ALGO,
        str(_ITERATIONS),
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    ])


def verify_password(password: str, stored: str | None) -> bool:
    """校验密码；账号无密码（如历史数据）时一律不通过。

    Args:
        password: 用户输入的明文密码。
        stored: 库中存储的哈希串，可能为 None（微信注册账号未设密码）。

    Returns:
        bool: 校验通过返回 True；格式非法 / 算法不符 / 不匹配均返回 False。
    """
    if not stored:
        return False
    try:
        # 哈希串固定四段：算法$迭代次数$盐$哈希值
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations)
        )
        # 常量时间比较，避免根据比较耗时推测哈希内容
        return hmac.compare_digest(base64.b64encode(dk).decode(), hash_b64)
    except (ValueError, TypeError):
        # 历史脏数据或格式被破坏时按校验失败处理
        return False


def create_token(user_id: int) -> str:
    """签发小程序用户 token。

    载荷包含 sub（用户 id）、iat（签发时间）、exp（过期时间），
    有效期取配置 jwt_expire_minutes（默认 30 天）。

    Args:
        user_id: users 表主键。

    Returns:
        str: JWT 字符串（HS256 签名）。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),   # JWT 规范要求 sub 为字符串
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def parse_token(token: str) -> int | None:
    """解析 token，返回 user_id；无效或过期返回 None。

    Args:
        token: 去掉 "Bearer " 前缀后的 JWT 字符串。

    Returns:
        int | None: 合法 token 返回用户 id；签名错误 / 过期 / 缺少 sub 返回 None。
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return int(sub) if sub is not None else None


def create_admin_token(admin_id: int) -> str:
    """管理后台 token：payload 带 typ=admin，与小程序端 token 隔离。

    Args:
        admin_id: admins 表主键。

    Returns:
        str: JWT 字符串，过期时间与小程序 token 相同策略。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(admin_id),
        "typ": "admin",        # 用途标记，解析时据此拒绝用户 token 冒充后台
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def parse_admin_token(token: str) -> int | None:
    """解析管理后台 token，返回 admin_id；typ 不对或无效返回 None。

    Args:
        token: 去掉 "Bearer " 前缀后的 JWT 字符串。

    Returns:
        int | None: 合法的后台 token 返回 admin_id；否则返回 None。
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    # 严格校验用途标记，用户 token 不能用于后台接口
    if payload.get("typ") != "admin":
        return None
    sub = payload.get("sub")
    return int(sub) if sub is not None else None
