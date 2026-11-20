"""tests/test_cache_scenarios.py — 3 小时缓存规则全场景测试

覆盖：
1. 3 小时有效期内重复查询同一地址天气、重复请求相同参数鱼情：
   上游天气源与大模型的实际调用次数都只有 1 次，返回数据与首次完全一致；
2. 超过 3 小时有效期后再次查询：自动重新调用接口、同步更新缓存并重置有效期；
3. 缓存键规则：地址标准化去空白/去重，参数哈希与字段顺序无关、不同条件不误命中。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_cache_scenarios.py`。
本文件通过 monkeypatch 替换 qweather / llm 模块的 enabled 与调用函数，
全程不访问真实网络，也不依赖 API Key。

注意事项：
- 计数桩对象的调用次数在 monkeypatch 生效期内有效，跨用例互不影响；
- 过期场景直接把库中 expire_at 改为过去时间，避免使用 sleep 等待。
"""

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import FishCache, WeatherCache
from app.services import cache_policy, llm, qweather

QUERY = {
    "province": "安徽省",
    "city": "合肥市",
    "district": "蜀山区",
    "date": "2026-11-19",
}


class QWeatherCounter:
    """记录上游天气源调用次数，第二次起返回不同温度，便于断言缓存已更新。"""

    def __init__(self) -> None:
        """初始化调用计数器为 0。"""
        self.calls = 0

    async def fetch(self, place: str) -> dict:
        """模拟和风天气抓取：首次返回 12℃，后续返回 25℃，其余字段固定。

        Args:
            place: 标准化地点字符串（本桩不参与计算，仅为对齐真实签名）。

        Returns:
            dict: 与 qweather.fetch_weather 同结构的观测字典。
        """
        self.calls += 1
        return {
            "condition": "晴",
            "condition_icon": "sun",
            "temp": 12 if self.calls == 1 else 25,
            "temp_max": 18 if self.calls == 1 else 30,
            "temp_min": 6 if self.calls == 1 else 18,
            "humidity": 60,
            "pressure": 1020,
            "wind_dir": "西北风",
            "wind_level": "3级",
            "wind_speed": 3.6,
            "sunrise": "06:40",
            "sunset": "17:10",
        }


class LLMCounter:
    """记录鱼情大模型调用次数。"""

    def __init__(self) -> None:
        """初始化调用计数器为 0。"""
        self.calls = 0

    async def chat(self, messages, **kwargs) -> str:
        """模拟大模型返回固定鱼情 JSON（score=82），并累加调用次数。

        Args:
            messages: 对话消息列表（本桩不消费，仅为对齐真实签名）。
            **kwargs: 其它调用参数（本桩忽略）。

        Returns:
            str: 可被 verdict.normalize 解析的 JSON 字符串。
        """
        self.calls += 1
        return (
            '{"score": 82, "species": ["鲫鱼", "鲤鱼"], '
            '"reason": "水温适宜、气压平稳，鱼口活跃", '
            '"advice": "早口占位打窝，主攻鲫鱼"}'
        )


def _patch_upstream(monkeypatch):
    """把天气源与大模型替换为计数桩，并强制标记为「已启用」。

    Args:
        monkeypatch: pytest 提供的补丁夹具，用例结束自动还原。

    Returns:
        tuple: (QWeatherCounter 实例, LLMCounter 实例)，用于断言调用次数与返回差异。
    """
    qw, lc = QWeatherCounter(), LLMCounter()
    monkeypatch.setattr(qweather, "enabled", lambda: True)
    monkeypatch.setattr(qweather, "fetch_weather", qw.fetch)
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat", lc.chat)
    return qw, lc


@pytest.mark.asyncio
async def test_repeat_query_within_3h_calls_upstream_once(client, session_factory, monkeypatch):
    """3 小时内重复查询：天气源与大模型各只调用 1 次，两次返回完全一致。

    前置条件：使用 QUERY 固定参数（安徽省 / 合肥市 / 蜀山区 / 2026-11-19），
    上游已替换为计数桩且尚未写入任何缓存。
    预期行为：第二次请求命中缓存，天气源与 LLM 调用次数均保持 1，
    返回的天气 / verdict / slots 与首次相同；库中两类缓存各 1 条，
    hit_count 各为 1，剩余有效期在 2 小时 59 分到 3 小时之间。
    断言意图：证明缓存既避免重复调用上游，又不会缩短或延长 TTL。
    """
    qw, lc = _patch_upstream(monkeypatch)

    r1 = await client.get("/api/weather", params=QUERY)
    assert r1.status_code == 200
    d1 = r1.json()["data"]

    r2 = await client.get("/api/weather", params=QUERY)
    assert r2.status_code == 200
    d2 = r2.json()["data"]

    # 核心断言：第三方接口只调用 1 次
    assert qw.calls == 1
    assert lc.calls == 1

    # 第二次返回的数据与首次一致
    assert d2["weather"]["temp"] == d1["weather"]["temp"] == 12
    assert d2["verdict"] == d1["verdict"]
    assert d2["slots"] == d1["slots"]
    assert d1["verdict"]["score"] == 82

    # 库里各只有 1 条缓存，命中次数 +1，有效期为 3 小时
    async with session_factory() as db:
        weather_rows = (await db.scalars(select(WeatherCache))).all()
        fish_rows = (await db.scalars(select(FishCache))).all()
        assert len(weather_rows) == 1
        assert len(fish_rows) == 1
        assert weather_rows[0].hit_count == 1
        assert fish_rows[0].hit_count == 1
        remaining = weather_rows[0].expire_at - dt.datetime.now()
        assert remaining > dt.timedelta(hours=2, minutes=59)
        assert remaining <= dt.timedelta(hours=3)


@pytest.mark.asyncio
async def test_expired_cache_refetches_and_resets_ttl(client, session_factory, monkeypatch):
    """超过 3 小时后：自动重新调用上游、更新缓存并重置有效期。

    前置条件：先成功查询一次生成缓存，再手工把两类缓存的 expire_at 改为 1 分钟前。
    预期行为：再次请求时上游调用次数变为 2，返回新温度 25℃（桩的第二次返回值），
    缓存条目仍各为 1 条（覆盖更新而非新增），剩余有效期恢复到约 3 小时。
    断言意图：验证过期失效与 TTL 续期的完整闭环。
    """
    qw, lc = _patch_upstream(monkeypatch)

    await client.get("/api/weather", params=QUERY)
    assert qw.calls == 1 and lc.calls == 1

    # 模拟 3 小时已过：把两条缓存的有效期改为 1 分钟前
    past = dt.datetime.now() - dt.timedelta(minutes=1)
    async with session_factory() as db:
        for row in (await db.scalars(select(WeatherCache))).all():
            row.expire_at = past
        for row in (await db.scalars(select(FishCache))).all():
            row.expire_at = past
        await db.commit()

    r2 = await client.get("/api/weather", params=QUERY)
    assert r2.status_code == 200
    d2 = r2.json()["data"]

    # 过期后重新调用上游，且数据为最新版本
    assert qw.calls == 2
    assert lc.calls == 2
    assert d2["weather"]["temp"] == 25

    # 缓存条目仍各为 1 条，有效期已被重置为 3 小时
    async with session_factory() as db:
        weather_rows = (await db.scalars(select(WeatherCache))).all()
        fish_rows = (await db.scalars(select(FishCache))).all()
        assert len(weather_rows) == 1 and len(fish_rows) == 1
        for row in (weather_rows[0], fish_rows[0]):
            remaining = row.expire_at - dt.datetime.now()
            assert remaining > dt.timedelta(hours=2, minutes=59)


def test_weather_cache_key_uses_normalized_address():
    """天气缓存键：空白/重复项被标准化，同一地址得到同一键。

    前置条件：无（纯函数调用，不涉及数据库）。
    预期行为：前后带空格的省份与原值等价；直辖市「省=市」只保留一次；
    不同区县或不同日期生成不同键。
    断言意图：保证同一实际地址不会因输入空格 / 重复而击穿缓存。
    """
    k1 = cache_policy.weather_cache_key(
        province="安徽省", city="合肥市", district="蜀山区", day="2026-11-19"
    )
    k2 = cache_policy.weather_cache_key(
        province=" 安徽省 ", city="合肥市", district="蜀山区", day="2026-11-19"
    )
    assert k1 == k2 == "安徽省 合肥市 蜀山区|2026-11-19"

    # 直辖市省=市只保留一次
    assert cache_policy.normalize_address("上海市", "上海市", "黄浦区") == "上海市 黄浦区"

    # 不同区 / 不同日期 → 不同键
    assert cache_policy.weather_cache_key(
        province="安徽省", city="合肥市", district="包河区", day="2026-11-19"
    ) != k1
    assert cache_policy.weather_cache_key(
        province="安徽省", city="合肥市", district="蜀山区", day="2026-11-20"
    ) != k1


def test_fish_cache_key_is_parameter_hash():
    """鱼情缓存键：与参数顺序无关的稳定哈希，不同参数不误命中。

    前置条件：无（纯函数调用）。a 与 b 为字段顺序不同的同一组参数。
    预期行为：键相等且以 "fish:" 前缀开头；仅修改 district 后键发生变化。
    断言意图：确认哈希基于规范化后的参数内容，而非字典遍历顺序。
    """
    a = dict(QUERY, place="安徽省 合肥市 蜀山区")
    b = {
        "place": "安徽省 合肥市 蜀山区",
        "district": "蜀山区",
        "city": "合肥市",
        "province": "安徽省",
        "date": "2026-11-19",
    }
    assert cache_policy.fish_cache_key(a) == cache_policy.fish_cache_key(b)
    assert cache_policy.fish_cache_key(a).startswith("fish:")

    changed = dict(b, district="包河区")
    assert cache_policy.fish_cache_key(changed) != cache_policy.fish_cache_key(a)


def test_ttl_defaults_to_three_hours():
    """默认缓存规则：天气与鱼情均为 3 小时。

    前置条件：无（读取 cache_policy.default_rules()，不读数据库配置）。
    预期行为：weather / fish 均为 10800 秒。
    断言意图：固化产品约定的 3 小时默认有效期，防止被静默改动。
    """
    rules = cache_policy.default_rules()
    assert rules["weather"] == 3 * 60 * 60
    assert rules["fish"] == 3 * 60 * 60
