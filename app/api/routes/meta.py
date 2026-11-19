"""meta.py — 基础数据（省市县 / 业务常量 / 广告素材）"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_db
from ...core.response import ok
from ...models import Province
from ...seed_data import AD_CREATIVES
from ...services import config_service

router = APIRouter(tags=["基础数据"])


@router.get("/cities", summary="省 / 市 / 区三级数据（全国 34 个省级行政区）")
async def cities(db: AsyncSession = Depends(get_db)):
    # cities / districts 均为 selectin 预加载，异步下不会触发隐式懒加载
    rows = await db.scalars(select(Province).order_by(Province.sort, Province.id))
    return ok(
        [
            {
                "name": p.name,
                "cities": [
                    {"name": c.name, "districts": [d.name for d in c.districts]}
                    for c in p.cities
                ],
            }
            for p in rows.all()
        ]
    )


@router.get("/config", summary="业务配置（签到积分 / 广告积分 / 会员套餐）")
async def config(db: AsyncSession = Depends(get_db)):
    """小程序端统一配置入口（公开接口，无需登录）。

    返回后台「积分配置」中设置的签到积分（会员 / 非会员）、广告积分规则，
    以及所有「已上架」的会员套餐，供解锁弹窗与开通页展示。
    后台修改配置后，小程序在缓存过期周期内即可拉取到最新值，无需发版。
    """
    return ok(await config_service.snapshot(db))


@router.get("/ads/creatives", summary="广告素材（占位投放）")
async def ad_creatives():
    return ok(AD_CREATIVES)
