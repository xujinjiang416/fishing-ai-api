"""models 包入口：统一导出所有数据表模型，供业务代码 `from ..models import Xxx` 使用。

说明：历史上存在的 `FeatureUnlock`（功能单次解锁：看广告/付费解锁）模型已随
「移除广告解锁」需求一并删除，全链路不再有该表与相关逻辑。
"""

from .models import (
    AdReward,
    Admin,
    AppConfig,
    CheckinRecord,
    City,
    District,
    Favorite,
    Goods,
    MembershipOrder,
    OperationLog,
    Order,
    Plan,
    PointsLog,
    Province,
    Spot,
    SpotComment,
    User,
    WeatherCache,
    WeatherLog,
)

__all__ = [
    "AdReward",
    "Admin",
    "AppConfig",
    "CheckinRecord",
    "City",
    "District",
    "Favorite",
    "Goods",
    "MembershipOrder",
    "OperationLog",
    "Order",
    "Plan",
    "PointsLog",
    "Province",
    "Spot",
    "SpotComment",
    "User",
    "WeatherCache",
    "WeatherLog",
]
