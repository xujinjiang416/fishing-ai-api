"""config_service.py — 系统积分配置服务（签到积分 / 广告积分等）

设计目标（对应需求 4、5）：
- 管理后台可可视化修改「签到积分（会员 / 非会员）」「观看广告积分」等规则；
- 规则持久化在 `app_configs` 表（键值对），不写死在代码里；
- 小程序端通过公开接口 `GET /api/config` 实时读取，后台改完即时生效；
- 服务端加一层短 TTL 内存缓存，降低高频读取对数据库的压力；配置一旦被修改会立即失效缓存。

对外提供：
- `snapshot(db)`：聚合为前端可直接消费的结构（camelCase + plans 一并返回）。
- `get_int(db, key)`：读取单个整数配置（带默认值兜底，避免配置缺失导致功能报错）。
- `apply_updates(db, updates)`：批量写入配置（后台保存时调用）。
- `clear_cache()`：清空内存缓存（后台写入后自动调用）。
"""

import time

from sqlalchemy import select

# 说明：这里只依赖会话类型做类型注解，运行时不做强校验，避免循环导入。
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models import AppConfig


class ConfigDef:
    """单个配置项的定义：默认值 / 类型 / 取值范围 / 说明。"""

    __slots__ = ("key", "default", "kind", "remark", "min", "max")

    def __init__(self, key: str, default, kind: str, remark: str, min=None, max=None):
        self.key = key
        self.default = default
        self.kind = kind        # "int" / "str"
        self.remark = remark
        self.min = min
        self.max = max


# 所有可配置项的元数据（后台「积分配置」页面据此渲染与校验）。
# 默认值取自 settings，保证「配置缺失」时有安全兜底，且与系统默认口径一致。
CONFIG_DEFS: dict[str, ConfigDef] = {
    "checkin_member_points": ConfigDef(
        "checkin_member_points", settings.checkin_member_points, "int", "会员每日签到可获得的积分", 0, 100000
    ),
    "checkin_normal_points": ConfigDef(
        "checkin_normal_points", settings.checkin_normal_points, "int", "非会员每日签到可获得的积分", 0, 100000
    ),
    "ad_points": ConfigDef("ad_points", settings.ad_points, "int", "观看单次广告可获得的积分", 0, 100000),
    "ad_seconds": ConfigDef("ad_seconds", settings.ad_seconds, "int", "单次广告时长（秒）", 5, 180),
    "ad_limit_normal": ConfigDef(
        "ad_limit_normal", settings.ad_limit_normal, "int", "非会员每日可观看广告的最大次数", 0, 1000
    ),
    "ad_limit_member": ConfigDef(
        "ad_limit_member", settings.ad_limit_member, "int", "会员每日可观看广告的最大次数", 0, 1000
    ),
    "sensitive_words": ConfigDef(
        "sensitive_words", "", "str", "评论敏感词（英文逗号分隔，为空时使用系统内置词库）"
    ),
}

# 键 -> 默认字符串值
DEFAULTS: dict[str, str] = {k: str(v.default) for k, v in CONFIG_DEFS.items()}

# ---- 服务端短 TTL 内存缓存：避免每次读库；write 时主动失效 ----
_CACHE_TTL = 15.0  # 秒
_cache: dict = {"at": 0.0, "raw": None}


def clear_cache() -> None:
    """清空内存缓存（配置被修改后必须调用）。"""
    _cache["at"] = 0.0
    _cache["raw"] = None


def _coerce(defn: ConfigDef, value: str):
    """把数据库里的字符串按定义转成期望类型；非法值回退默认值（保证功能不报错）。"""
    if defn.kind == "int":
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            return defn.default
        if defn.min is not None and n < defn.min:
            return defn.min
        if defn.max is not None and n > defn.max:
            return defn.max
        return n
    return value if value is not None else defn.default


async def _raw_map(db: AsyncSession) -> dict[str, str]:
    """读取全部配置（带缓存），缺失的键用默认值补齐。"""
    now = time.time()
    if _cache["raw"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["raw"]

    rows = (await db.scalars(select(AppConfig))).all()
    raw = dict(DEFAULTS)  # 先铺默认值，保证任何键存在
    for r in rows:
        raw[r.key] = r.value
    _cache["raw"] = raw
    _cache["at"] = now
    return raw


async def get_int(db: AsyncSession, key: str) -> int:
    """读取单个整数配置；键不存在或值非法时返回默认值。"""
    defn = CONFIG_DEFS.get(key)
    raw = await _raw_map(db)
    if defn is None:
        # 未登记的键：尽力解析为整数，失败则 0
        try:
            return int(str(raw.get(key, "0")).strip())
        except (TypeError, ValueError):
            return 0
    return _coerce(defn, raw.get(key, DEFAULTS[key]))


async def get_str(db: AsyncSession, key: str) -> str:
    """读取单个字符串配置；键不存在时返回默认值。"""
    raw = await _raw_map(db)
    return raw.get(key, DEFAULTS.get(key, ""))


async def config_values(db: AsyncSession) -> dict[str, int]:
    """读取全部「业务配置」的强类型快照（供内部逻辑直接使用）。"""
    raw = await _raw_map(db)
    out: dict[str, int] = {}
    for key, defn in CONFIG_DEFS.items():
        if defn.kind == "int":
            out[key] = _coerce(defn, raw.get(key, DEFAULTS[key]))
    return out


async def snapshot(db: AsyncSession) -> dict:
    """聚合成前端可直接消费的结构（camelCase）。

    小程序端只需读取本结构即可获得：签到积分规则、广告积分规则、会员套餐列表。
    """
    v = await config_values(db)
    # 仅返回「上架」的会员套餐，按排序值升序；数据库异常时返回空列表兜底
    plans = []
    try:
        from ..models import Plan  # 局部导入，避免模块级循环依赖

        rows = (
            await db.scalars(
                select(Plan).where(
                    (Plan.status.is_(None)) | (Plan.status == "on")
                ).order_by(Plan.sort, Plan.id)
            )
        ).all()
        plans = [
            {
                "id": p.id,
                "name": p.name,
                "duration": p.duration,
                "durationLabel": p.duration_label,
                "price": p.price,
                "origPrice": p.orig_price,
                "icon": p.icon,
                "tag": p.tag,
                "tagType": p.tag_type,
                "benefits": p.benefits or [],
            }
            for p in rows
        ]
    except Exception:
        plans = []

    return {
        "pointsPerYuan": settings.points_per_yuan,
        "checkin": {
            "memberPoints": v.get("checkin_member_points", 5),
            "normalPoints": v.get("checkin_normal_points", 1),
        },
        "ad": {
            "points": v.get("ad_points", 5),
            "seconds": v.get("ad_seconds", 30),
            "limitNormal": v.get("ad_limit_normal", 15),
            "limitMember": v.get("ad_limit_member", 20),
        },
        "plans": plans,
    }


async def apply_updates(db: AsyncSession, updates: dict) -> list[tuple[str, str, str]]:
    """批量写入配置（upsert），返回 [(key, old, new)] 变更明细，供操作日志记录。

    updates 允许包含 int / str / None；None 表示该键本次不修改。
    调用方负责 `await db.commit()`；本函数不做提交，保证与业务写入同事务。
    """
    changes: list[tuple[str, str, str]] = []
    raw = await _raw_map(db)
    for key, new_val in updates.items():
        defn = CONFIG_DEFS.get(key)
        if defn is None or new_val is None:
            continue
        old = raw.get(key, DEFAULTS.get(key, ""))
        new_str = str(int(new_val)) if defn.kind == "int" else str(new_val)
        if str(old) == new_str:
            continue  # 无变化不落库、不记日志
        row = await db.get(AppConfig, key)
        if row is None:
            db.add(AppConfig(key=key, value=new_str, remark=defn.remark))
        else:
            row.value = new_str
            row.remark = defn.remark
        changes.append((key, str(old), new_str))
    clear_cache()  # 配置变更后立即失效缓存，确保下次读取到最新值
    return changes
