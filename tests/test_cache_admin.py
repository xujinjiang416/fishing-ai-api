"""tests/test_cache_admin.py — 缓存管理接口测试

覆盖：天气/鱼情缓存列表 / 详情 / 状态筛选 / 规则读写 / 统计 / 删除 / 权限拦截（普通管理员 403）。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_cache_admin.py`。

注意事项：
- 缓存数据由各用例通过 _seed_caches / _seed_fish_caches 现场写入，不使用 seeded 的缓存；
- 过期与否按 expire_at 与当前时间比较，夹具用 now ± 小时构造，避免时间边界抖动；
- 缓存规则（TTL）默认均取自 cache_policy 的 3 小时默认值。
"""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_admin_token, hash_password
from app.models import Admin, FishCache, WeatherCache


async def _seed_caches(session_factory) -> None:
    """写入两条天气缓存：一条有效（合肥蜀山区，命中 2 次）、一条已过期（上海黄浦区）。

    Args:
        session_factory: 与测试库绑定的 async_sessionmaker。

    Returns:
        None。数据已 commit，可直接被接口读取。
    """
    now = datetime.now()
    async with session_factory() as db:
        db.add_all(
            [
                WeatherCache(
                    region="合肥市蜀山区",
                    day="2026-11-19",
                    source="qweather",
                    size_bytes=100,
                    hit_count=2,
                    expire_at=now + timedelta(hours=2),
                    payload={"weather": {"place": "安徽省 合肥市 蜀山区", "temp": 12, "condition": "阴转晴"}, "verdict": {}},
                ),
                WeatherCache(
                    region="上海市黄浦区",
                    day="2026-11-19",
                    source="llm",
                    size_bytes=200,
                    hit_count=0,
                    expire_at=now - timedelta(hours=1),
                    payload={"weather": {"place": "上海市 上海市 黄浦区", "temp": 10, "condition": "多云"}, "verdict": {}},
                ),
            ]
        )
        await db.commit()


@pytest.mark.asyncio
async def test_list_and_detail(client, session_factory, admin_headers):
    """覆盖天气缓存列表分页与单条详情。

    前置条件：写入 2 条天气缓存。
    预期行为：列表 total=2；按列表首条 id 查详情返回 200，
    payload.weather.temp 为 12 或 10（与种子一致）。
    断言意图：确认列表与详情使用同一批数据、payload 原样返回。
    """
    await _seed_caches(session_factory)
    r = await client.get("/api/admin/cache", headers=admin_headers, params={"page": 1, "pageSize": 10})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 2

    cid = data["list"][0]["id"]
    r2 = await client.get(f"/api/admin/cache/{cid}", headers=admin_headers)
    assert r2.status_code == 200
    assert r2.json()["data"]["payload"]["weather"]["temp"] in (12, 10)


@pytest.mark.asyncio
async def test_filter_by_status(client, session_factory, admin_headers):
    """覆盖按状态（status=expired）筛选天气缓存。

    前置条件：2 条缓存中仅上海黄浦区一条 expire_at 已过期。
    预期行为：total=1 且 region 为「上海市黄浦区」。
    断言意图：确认过期判定基于 expire_at 而非写入时间。
    """
    await _seed_caches(session_factory)
    r = await client.get("/api/admin/cache", headers=admin_headers, params={"status": "expired"})
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["list"][0]["region"] == "上海市黄浦区"


@pytest.mark.asyncio
async def test_rules_get_and_put(client, admin_headers):
    """覆盖缓存规则（TTL 秒）的读取与更新。

    前置条件：使用 seeded 的默认配置（未额外写入缓存记录）。
    预期行为：默认天气 / 鱼情 TTL 均为 10800 秒（3 小时）；
    PUT 自定义值后立即回显新值。
    断言意图：确认后台可在线调整缓存有效期并即时生效。
    """
    r = await client.get("/api/admin/cache/rules", headers=admin_headers)
    assert r.status_code == 200
    rules = r.json()["data"]
    # 默认天气 / 鱼情有效期均为 3 小时
    assert rules["weather"] == 10800
    assert rules["fish"] == 10800

    r2 = await client.put(
        "/api/admin/cache/rules",
        headers=admin_headers,
        json={"weather": 7200, "fish": 5400},
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["weather"] == 7200
    assert r2.json()["data"]["fish"] == 5400


@pytest.mark.asyncio
async def test_stats(client, session_factory, admin_headers):
    """覆盖仅含天气缓存时的统计口径。

    前置条件：2 条天气缓存（size_bytes 分别为 100 / 200）。
    预期行为：totalCount=2、totalSizeBytes=300。
    断言意图：确认统计按条数与字节数聚合。
    """
    await _seed_caches(session_factory)
    r = await client.get("/api/admin/cache/stats", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["totalCount"] == 2
    assert data["totalSizeBytes"] == 300


@pytest.mark.asyncio
async def test_delete_and_batch_delete(client, session_factory, admin_headers):
    """覆盖单条删除与批量删除缓存。

    前置条件：写入 2 条天气缓存，先取出全部 id。
    预期行为：删除一条后 total=1；再批量删除剩余一条后 total=0。
    断言意图：确认两种删除方式都能真实落库。
    """
    await _seed_caches(session_factory)
    r = await client.get("/api/admin/cache", headers=admin_headers)
    ids = [it["id"] for it in r.json()["data"]["list"]]

    d = await client.delete(f"/api/admin/cache/{ids[0]}", headers=admin_headers)
    assert d.status_code == 200
    assert (await client.get("/api/admin/cache", headers=admin_headers)).json()["data"]["total"] == 1

    b = await client.post("/api/admin/cache/batch-delete", headers=admin_headers, json={"ids": [ids[1]]})
    assert b.status_code == 200
    assert (await client.get("/api/admin/cache", headers=admin_headers)).json()["data"]["total"] == 0


async def _seed_fish_caches(session_factory) -> None:
    """写入两条鱼情缓存：一条有效（合肥蜀山区，score=82）、一条已过期（上海黄浦区，score=55）。

    Args:
        session_factory: 与测试库绑定的 async_sessionmaker。

    Returns:
        None。cache_key 为 64 位哈希占位，region 为标准化地址，供关键词搜索使用。
    """
    now = datetime.now()
    async with session_factory() as db:
        db.add_all(
            [
                FishCache(
                    cache_key="fish:" + "a" * 64,
                    region="安徽省 合肥市 蜀山区",
                    params={
                        "province": "安徽省",
                        "city": "合肥市",
                        "district": "蜀山区",
                        "date": "2026-11-19",
                        "place": "安徽省 合肥市 蜀山区",
                    },
                    payload={
                        "verdict": {"score": 82, "name": "适合出钓", "level": "good", "advice": "早口打窝"},
                        "slots": [],
                        "aiGenerated": True,
                    },
                    source="llm",
                    expire_at=now + timedelta(hours=2),
                    size_bytes=150,
                ),
                FishCache(
                    cache_key="fish:" + "b" * 64,
                    region="上海市 黄浦区",
                    params={"province": "上海市", "district": "黄浦区", "date": "2026-11-19"},
                    payload={"verdict": {"score": 55, "name": "一般", "level": "fair"}, "slots": []},
                    source="rule",
                    expire_at=now - timedelta(hours=1),
                    size_bytes=90,
                ),
            ]
        )
        await db.commit()


@pytest.mark.asyncio
async def test_fish_cache_list_detail_and_delete(client, session_factory, admin_headers):
    """覆盖鱼情缓存列表、状态筛选、关键词搜索、详情与删除。

    前置条件：写入 2 条鱼情缓存（合肥 / 上海，后者已过期）。
    预期行为：列表 total=2；status=expired 与 keyword=合肥 各命中 1 条；
    详情返回 payload.verdict.score（82 或 55）；删除一条后 total=1。
    断言意图：确认鱼情缓存与天气缓存具备同等管理能力，关键词按 region 匹配。
    """
    await _seed_fish_caches(session_factory)

    r = await client.get("/api/admin/cache/fish", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 2

    expired = await client.get(
        "/api/admin/cache/fish", headers=admin_headers, params={"status": "expired"}
    )
    assert expired.json()["data"]["total"] == 1

    searched = await client.get(
        "/api/admin/cache/fish", headers=admin_headers, params={"keyword": "合肥"}
    )
    assert searched.json()["data"]["total"] == 1

    fid = data["list"][0]["id"]
    detail = await client.get(f"/api/admin/cache/fish/{fid}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["payload"]["verdict"]["score"] in (82, 55)

    d = await client.delete(f"/api/admin/cache/fish/{fid}", headers=admin_headers)
    assert d.status_code == 200
    assert (
        await client.get("/api/admin/cache/fish", headers=admin_headers)
    ).json()["data"]["total"] == 1


@pytest.mark.asyncio
async def test_stats_includes_fish_cache(client, session_factory, admin_headers):
    """覆盖统计同时聚合天气与鱼情缓存。

    前置条件：天气 2 条（300 字节）+ 鱼情 2 条（150 + 90 字节）。
    预期行为：weatherCount=2、fishCount=2、totalCount=4、totalSizeBytes=540。
    断言意图：确认统计页的分类计数与总量一致。
    """
    await _seed_caches(session_factory)
    await _seed_fish_caches(session_factory)
    data = (await client.get("/api/admin/cache/stats", headers=admin_headers)).json()["data"]
    assert data["weatherCount"] == 2
    assert data["fishCount"] == 2
    assert data["totalCount"] == 4
    assert data["totalSizeBytes"] == 300 + 240


@pytest.mark.asyncio
async def test_normal_admin_forbidden(client, session_factory):
    """覆盖普通管理员（非超级管理员）的缓存管理权限。

    前置条件：新增 id=2、角色「普通管理员」的账号及一条缓存。
    预期行为：查看列表允许（200）；修改规则 / 删除缓存 / 刷新缓存 /
    删除鱼情缓存等高风险操作一律 403。
    断言意图：确认缓存写操作仅超级管理员可用，读操作不受限。
    """
    # 额外添加普通管理员（id=2）+ 一条缓存
    async with session_factory() as db:
        db.add(
            Admin(
                id=2,
                username="viewer",
                password_hash=hash_password("x"),
                name="普通管理员",
                role="普通管理员",
                avatar_text="普",
            )
        )
        db.add(
            WeatherCache(
                region="测试区",
                day="2026-11-19",
                source="qweather",
                payload={"weather": {"place": "安徽省 测试市 测试区"}},
            )
        )
        await db.commit()

    h = {"Authorization": "Bearer " + create_admin_token(2)}
    # 查看允许
    assert (await client.get("/api/admin/cache", headers=h)).status_code == 200
    # 高危操作拦截
    assert (
        await client.put("/api/admin/cache/rules", headers=h, json={"weather": 3600, "fish": 3600})
    ).status_code == 403
    assert (await client.delete("/api/admin/cache/1", headers=h)).status_code == 403
    assert (await client.post("/api/admin/cache/1/refresh", headers=h)).status_code == 403
    assert (await client.delete("/api/admin/cache/fish/1", headers=h)).status_code == 403