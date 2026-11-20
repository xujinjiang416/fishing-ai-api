"""models 包入口：统一导出所有数据表模型，供业务代码 `from ..models import Xxx` 使用。

职责：
    1. 汇总 app/models/models.py 中定义的全部 ORM 模型，收敛外部导入路径；
    2. 通过 __all__ 明确公开模型清单，避免 `from ..models import *` 时污染命名空间。

依赖：models.models（真正的表结构定义）与 core.database.Base（声明式基类）。

注意事项：
    - 新增数据表时需同时在本文件导入并加入 __all__，否则其他模块无法从包入口引用；
    - 表结构与表间关系（外键、级联删除）均在 models.py 中说明。

说明：历史上存在的 `FeatureUnlock`（功能单次解锁：看广告/付费解锁）模型已随
「移除广告解锁」需求一并删除，全链路不再有该表与相关逻辑。
"""

# 逐个导入模型：保证「导入 app.models」时所有表都已注册到 Base.metadata（建表依赖）
from .models import (
    AdReward,
    Admin,
    AppConfig,
    CheckinRecord,
    City,
    District,
    Favorite,
    FishCache,
    Goods,
    MembershipOrder,
    OperationLog,
    Order,
    OrderLog,
    Plan,
    PointsLog,
    Province,
    Spot,
    SpotComment,
    User,
    UserAddress,
    WeatherCache,
    WeatherLog,
)

# 对外公开的模型清单：决定 `from app.models import *` 可导出的类型
__all__ = [
    "AdReward",
    "Admin",
    "AppConfig",
    "CheckinRecord",
    "City",
    "District",
    "Favorite",
    "FishCache",
    "Goods",
    "MembershipOrder",
    "OperationLog",
    "Order",
    "OrderLog",
    "Plan",
    "PointsLog",
    "Province",
    "Spot",
    "SpotComment",
    "User",
    "UserAddress",
    "WeatherCache",
    "WeatherLog",
]
