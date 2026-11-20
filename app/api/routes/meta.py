"""meta.py — 基础数据（省市县 / 业务常量 / 广告素材）

职责：为小程序端提供与个人身份无关的公共数据：省 / 市 / 区三级行政区划、
业务配置快照（签到积分、广告积分、会员套餐）、广告素材占位数据。

鉴权要求：全部为公开接口，无需登录，也不校验管理员身份。

依赖服务：MySQL（provinces / cities / districts / app_configs / plans 等表）、
services.config_service（业务配置聚合 + 15 秒短 TTL 内存缓存）。

注意事项：配置类数据由管理后台维护，修改后服务端内存缓存会自动失效，
小程序在缓存周期内即可拉取到最新值，无需发版。
"""

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
    """省 / 市 / 区三级联动数据。

    方法与路径：GET /cities（公开）。
    响应：数组，每项 { name: 省名, cities: [{ name: 市名, districts: [区名...] }] }，
    按 sort、id 升序返回，供前端城市选择器直接消费。
    说明：查询结果只读，不做分页。
    """
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

    方法与路径：GET /config（公开）。
    响应：pointsPerYuan（积分兑元比例）、checkin（memberPoints / normalPoints）、
    ad（points / seconds / limitNormal / limitMember）、plans（已上架套餐数组）。
    """
    return ok(await config_service.snapshot(db))


@router.get("/ads/creatives", summary="广告素材（占位投放）")
async def ad_creatives():
    """广告素材列表（占位投放）。

    方法与路径：GET /ads/creatives（公开）。
    响应：seed_data.AD_CREATIVES 中的静态素材数组，供前端广告位占位展示；
    不查库、无缓存，数据随代码版本发布。
    """
    return ok(AD_CREATIVES)
