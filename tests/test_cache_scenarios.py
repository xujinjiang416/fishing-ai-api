"""tests/test_cache_scenarios.py — 3 小时缓存规则全场景测试

覆盖：
1. 3 小时有效期内重复查询同一地址天气、重复请求相同参数鱼情：
   上游天气源与大模型的实际调用次数都只有 1 次，返回数据与首次完全一致；
2. 超过 3 小时有效期后再次查询：自动重新调用接口、同步更新缓存并重置有效期；
3. 缓存键规则：地址标准化去空白/去重，参数哈希与字段顺序无关、不同条件不误命中。
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
        self.calls = 0

    async def fetch(self, place: str) -> dict:
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
        self.calls = 0

    async def chat(self, messages, **kwargs) -> str:
        self.calls += 1
        return (
            '{"score": 82, "species": ["鲫鱼", "鲤鱼"], '
            '"reason": "水温适宜、气压平稳，鱼口活跃", '
            '"advice": "早口占位打窝，主攻鲫鱼"}'
        )


def _patch_upstream(monkeypatch):
    qw, lc = QWeatherCounter(), LLMCounter()
    monkeypatch.setattr(qweather, "enabled", lambda: True)
    monkeypatch.setattr(qweather, "fetch_weather", qw.fetch)
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat", lc.chat)
    return qw, lc


@pytest.mark.asyncio
async def test_repeat_query_within_3h_calls_upstream_once(client, session_factory, monkeypatch):
    """3 小时内重复查询：天气源与大模型各只调用 1 次，两次返回完全一致。"""
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
    """超过 3 小时后：自动重新调用上游、更新缓存并重置有效期。"""
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
    """天气缓存键：空白/重复项被标准化，同一地址得到同一键。"""
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
    """鱼情缓存键：与参数顺序无关的稳定哈希，不同参数不误命中。"""
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
    """默认缓存规则：天气与鱼情均为 3 小时。"""
    rules = cache_policy.default_rules()
    assert rules["weather"] == 3 * 60 * 60
    assert rules["fish"] == 3 * 60 * 60
