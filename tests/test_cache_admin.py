"""tests/test_cache_admin.py — 缓存管理接口测试

覆盖：天气/鱼情缓存列表 / 详情 / 状态筛选 / 规则读写 / 统计 / 删除 / 权限拦截（普通管理员 403）。
"""

from datetime import datetime, timedelta

import pytest

from app.core.security import create_admin_token, hash_password
from app.models import Admin, FishCache, WeatherCache


async def _seed_caches(session_factory) -> None:
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
    await _seed_caches(session_factory)
    r = await client.get("/api/admin/cache", headers=admin_headers, params={"status": "expired"})
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["list"][0]["region"] == "上海市黄浦区"


@pytest.mark.asyncio
async def test_rules_get_and_put(client, admin_headers):
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
    await _seed_caches(session_factory)
    r = await client.get("/api/admin/cache/stats", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["totalCount"] == 2
    assert data["totalSizeBytes"] == 300


@pytest.mark.asyncio
async def test_delete_and_batch_delete(client, session_factory, admin_headers):
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
    await _seed_caches(session_factory)
    await _seed_fish_caches(session_factory)
    data = (await client.get("/api/admin/cache/stats", headers=admin_headers)).json()["data"]
    assert data["weatherCount"] == 2
    assert data["fishCount"] == 2
    assert data["totalCount"] == 4
    assert data["totalSizeBytes"] == 300 + 240


@pytest.mark.asyncio
async def test_normal_admin_forbidden(client, session_factory):
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