"""cache_policy.py — 天气缓存过期策略（城市等级 + 数据精度差异化 TTL）

缓存过期时长不再写死，而是由「缓存过期规则」按两个维度动态计算：
- 城市等级：一线 / 新一线 / 二线 / 三线（内置映射，其余默认三线）
- 数据精度：province / city / district（区最细、省最粗）

规则持久化在 app_configs 表（key=cache_ttl_rules，JSON），可在后台可视化配置；
未配置时用 DEFAULT_RULES 兜底。最终 TTL 取两个维度中的较小值（更精细 / 更一线 = 更新时间更短）。
"""

import json
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppConfig

RULES_KEY = "cache_ttl_rules"
RULES_REMARK = "天气缓存过期规则（城市等级/数据精度差异化 TTL，单位秒）"

# 默认规则（秒）
DEFAULT_RULES: dict = {
    "cityLevels": {"一线": 3600, "新一线": 7200, "二线": 10800, "三线": 14400},
    "precisions": {"province": 14400, "city": 10800, "district": 7200},
}

# 内置城市等级映射（覆盖一线/新一线/二线主要城市，其余归三线）
CITY_LEVELS: dict[str, list[str]] = {
    "一线": ["北京市", "上海市", "广州市", "深圳市"],
    "新一线": ["成都市", "杭州市", "重庆市", "武汉市", "西安市", "苏州市", "南京市",
               "郑州市", "长沙市", "天津市", "青岛市", "沈阳市", "宁波市", "东莞市", "合肥市"],
    "二线": ["厦门市", "福州市", "济南市", "大连市", "无锡市", "佛山市", "温州市",
             "南宁市", "昆明市", "南昌市", "贵阳市", "哈尔滨市", "长春市", "石家庄市",
             "太原市", "泉州市", "南通市", "常州市", "徐州市", "嘉兴市", "绍兴市",
             "兰州市", "乌鲁木齐市", "中山市", "珠海市", "惠州市", "台州市", "烟台市", "潍坊市"],
}

_LEVEL_INDEX: dict[str, str] = {}
for _lv, _cities in CITY_LEVELS.items():
    for _c in _cities:
        _LEVEL_INDEX[_c] = _lv

DEFAULT_TTL = 2 * 60 * 60  # 兜底：两维度都未配置时用 2 小时


def _norm_city(city: str | None) -> str:
    """统一补「市」后缀，兼容传入「合肥」/「合肥市」。"""
    c = (city or "").strip()
    if not c:
        return ""
    return c if c.endswith("市") else c + "市"


def city_level(city: str | None) -> str:
    return _LEVEL_INDEX.get(_norm_city(city), "三线")


def precision_of(*, province: str | None = None, city: str | None = None, district: str | None = None) -> str:
    if district:
        return "district"
    if city:
        return "city"
    if province:
        return "province"
    return "district"  # 兜底按最细精度


def default_rules() -> dict:
    return deepcopy(DEFAULT_RULES)


async def get_rules(db: AsyncSession) -> dict:
    """读取过期规则；缺失/非法配置时回退默认规则。"""
    row = await db.scalar(select(AppConfig).where(AppConfig.key == RULES_KEY))
    rules = default_rules()
    if not row or not row.value:
        return rules
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return rules
    if isinstance(data, dict):
        for section in ("cityLevels", "precisions"):
            src = data.get(section)
            if isinstance(src, dict):
                for k, v in src.items():
                    try:
                        rules[section][k] = int(v)
                    except (TypeError, ValueError):
                        continue
    return rules


async def save_rules(db: AsyncSession, rules: dict) -> None:
    """写入过期规则（调用方负责 commit）。"""
    payload = json.dumps(rules, ensure_ascii=False)
    row = await db.scalar(select(AppConfig).where(AppConfig.key == RULES_KEY))
    if row is None:
        db.add(AppConfig(key=RULES_KEY, value=payload, remark=RULES_REMARK))
    else:
        row.value = payload
        row.remark = RULES_REMARK


async def ttl_for(db: AsyncSession, *, province=None, city=None, district=None) -> int:
    """计算某地区查询的缓存 TTL（秒）：城市等级与数据精度两维度取较小值。"""
    rules = await get_rules(db)
    lv = city_level(city)
    prec = precision_of(province=province, city=city, district=district)

    candidates: list[int] = []
    if lv in rules["cityLevels"]:
        candidates.append(int(rules["cityLevels"][lv]))
    if prec in rules["precisions"]:
        candidates.append(int(rules["precisions"][prec]))
    return min(candidates) if candidates else DEFAULT_TTL