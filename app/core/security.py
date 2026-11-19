"""security.py — JWT 签发与校验 + 密码哈希

小程序侧登录态收敛为一个 Bearer Token：账号密码登录（或微信登录）换 token，
后续所有需要身份的接口都带 `Authorization: Bearer <token>`。
"""

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from .config import settings

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    """生成密码哈希（pbkdf2-sha256 + 随机盐），格式：algo$iterations$salt$hash"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join([
        _ALGO,
        str(_ITERATIONS),
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    ])


def verify_password(password: str, stored: str | None) -> bool:
    """校验密码；账号无密码（如历史数据）时一律不通过。"""
    if not stored:
        return False
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations)
        )
        return hmac.compare_digest(base64.b64encode(dk).decode(), hash_b64)
    except (ValueError, TypeError):
        return False


def create_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def parse_token(token: str) -> int | None:
    """解析 token，返回 user_id；无效或过期返回 None。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return int(sub) if sub is not None else None


def create_admin_token(admin_id: int) -> str:
    """管理后台 token：payload 带 typ=admin，与小程序端 token 隔离。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(admin_id),
        "typ": "admin",
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def parse_admin_token(token: str) -> int | None:
    """解析管理后台 token，返回 admin_id；typ 不对或无效返回 None。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "admin":
        return None
    sub = payload.get("sub")
    return int(sub) if sub is not None else None
