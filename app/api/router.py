"""router.py — 汇总所有业务路由（统一挂载在 /api 前缀下）

职责：
    创建顶层 APIRouter，按业务域依次 include 各子路由模块，
    最终由 app/main.py 以 settings.api_prefix（默认 /api）挂载到应用。

依赖：
    - routes.auth（登录/注册/微信登录）、routes.user（资料/签到/积分/地址/收藏）；
    - routes.meta（省市区域、配置等元数据）、routes.weather（天气与鱼情 AI 研判）；
    - routes.spots（野塘列表/详情/评论）、routes.shop（商品/订单/会员购买）；
    - routes.rewards（广告奖励）、routes.admin（管理后台）、routes.cache_admin（缓存运维）。

注意事项：
    - 子路由内部的 path 均以自身业务前缀书写（如 /auth/login），此处不再加前缀；
    - 新增业务模块时在此登记一行即可，注意避免同路径重复注册导致路由遮蔽。
"""

from fastapi import APIRouter

from .routes import admin, auth, cache_admin, meta, rewards, shop, spots, user, weather

# 顶层路由容器：所有子路由统一挂到该实例
api_router = APIRouter()
api_router.include_router(auth.router)          # 登录 / 注册 / 微信登录
api_router.include_router(user.router)          # 用户资料 / 签到 / 积分 / 地址 / 收藏
api_router.include_router(meta.router)          # 省市区、系统配置等元数据
api_router.include_router(weather.router)       # 天气查询与鱼情 AI 研判
api_router.include_router(spots.router)         # 野塘列表 / 详情 / 评论
api_router.include_router(shop.router)          # 商品 / 订单 / 会员
api_router.include_router(rewards.router)       # 广告奖励
api_router.include_router(admin.router)         # 管理后台
api_router.include_router(cache_admin.router)   # 缓存运维（刷新 / 清理）
