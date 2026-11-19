"""router.py — 汇总所有业务路由（统一挂载在 /api 前缀下）"""

from fastapi import APIRouter

from .routes import admin, auth, cache_admin, meta, rewards, shop, spots, user, weather

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(user.router)
api_router.include_router(meta.router)
api_router.include_router(weather.router)
api_router.include_router(spots.router)
api_router.include_router(shop.router)
api_router.include_router(rewards.router)
api_router.include_router(admin.router)
api_router.include_router(cache_admin.router)
