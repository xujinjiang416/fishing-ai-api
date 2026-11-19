"""test_addresses.py — 收货地址管理与支付前地址校验

覆盖：新增（首条自动默认）/ 编辑 / 设为默认 / 删除默认后自动顺延 /
越权隔离 / 字段校验 / 下单必须携带本人地址并快照到订单。
"""

import pytest
import pytest_asyncio

from app.models import City, District, Goods, Province


@pytest_asyncio.fixture
async def geo(session_factory, seeded):
    """最小行政区划数据，用于校验省市区合法性。"""
    async with session_factory() as db:
        p = Province(name="浙江省", sort=1)
        db.add(p)
        await db.flush()
        c = City(province_id=p.id, name="杭州市", sort=1)
        db.add(c)
        await db.flush()
        db.add(District(city_id=c.id, name="西湖区", sort=1))
        await db.commit()
    yield


@pytest_asyncio.fixture
async def goods(session_factory, seeded):
    async with session_factory() as db:
        db.add(
            Goods(
                id="g9",
                name="测试商品",
                cat="工具",
                price=50.0,
                orig=60.0,
                img=None,
                sales="0",
                tags=[],
                spec="标准",
                stock=10,
                desc="测试",
                status="on",
            )
        )
        await db.commit()
    yield


def addr_payload(**overrides):
    data = {
        "receiver": "张三",
        "phone": "13800138000",
        "province": "浙江省",
        "city": "杭州市",
        "district": "西湖区",
        "detail": "文三路 100 号 1 幢 101",
        "isDefault": False,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_address_crud_and_default_logic(client, normal_headers):
    # 第一条地址自动设为默认
    r = await client.post("/api/user/addresses", headers=normal_headers, json=addr_payload())
    assert r.json()["code"] == 0
    a1 = r.json()["data"]
    assert a1["isDefault"] is True
    assert a1["fullAddress"] == "浙江省杭州市西湖区文三路 100 号 1 幢 101"

    # 第二条不默认
    r = await client.post(
        "/api/user/addresses",
        headers=normal_headers,
        json=addr_payload(receiver="李四", detail="滨江路 8 号"),
    )
    a2 = r.json()["data"]
    assert a2["isDefault"] is False

    # 列表默认地址置顶
    r = await client.get("/api/user/addresses", headers=normal_headers)
    items = r.json()["data"]
    assert [x["id"] for x in items] == [a1["id"], a2["id"]]

    # 编辑全字段
    r = await client.put(
        f"/api/user/addresses/{a2['id']}",
        headers=normal_headers,
        json=addr_payload(receiver="李四改", detail="滨江路 9 号", isDefault=False),
    )
    assert r.json()["data"]["receiver"] == "李四改"
    assert r.json()["data"]["detail"] == "滨江路 9 号"

    # 设为默认：原默认被取消
    r = await client.post(f"/api/user/addresses/{a2['id']}/default", headers=normal_headers)
    assert r.json()["data"]["isDefault"] is True
    r = await client.get(f"/api/user/addresses/{a1['id']}", headers=normal_headers)
    assert r.json()["data"]["isDefault"] is False

    # 删除默认地址：剩余第一条自动成为默认
    r = await client.delete(f"/api/user/addresses/{a2['id']}", headers=normal_headers)
    assert r.json()["data"]["defaultId"] == a1["id"]
    r = await client.get("/api/user/addresses", headers=normal_headers)
    assert r.json()["data"][0]["id"] == a1["id"]
    assert r.json()["data"][0]["isDefault"] is True


@pytest.mark.asyncio
async def test_address_validation_and_ownership(client, normal_headers, member_headers):
    # 手机号非法 -> 422
    r = await client.post(
        "/api/user/addresses", headers=normal_headers, json=addr_payload(phone="123")
    )
    assert r.status_code == 422

    # 必填字段为空 -> 422
    r = await client.post(
        "/api/user/addresses", headers=normal_headers, json=addr_payload(detail="   ")
    )
    assert r.status_code == 422

    r = await client.post("/api/user/addresses", headers=normal_headers, json=addr_payload())
    aid = r.json()["data"]["id"]

    # 其他用户不可读写
    assert (await client.get(f"/api/user/addresses/{aid}", headers=member_headers)).status_code == 404
    assert (
        await client.put(
            f"/api/user/addresses/{aid}", headers=member_headers, json=addr_payload()
        )
    ).status_code == 404
    assert (
        await client.delete(f"/api/user/addresses/{aid}", headers=member_headers)
    ).status_code == 404

    # 未登录不可访问
    assert (await client.get("/api/user/addresses")).status_code == 401


@pytest.mark.asyncio
async def test_address_region_validation(client, normal_headers, geo):
    # 合法省市区
    r = await client.post("/api/user/addresses", headers=normal_headers, json=addr_payload())
    assert r.json()["code"] == 0

    # 非法省份
    r = await client.post(
        "/api/user/addresses",
        headers=normal_headers,
        json=addr_payload(province="不存在省"),
    )
    assert r.json()["code"] == 1001
    assert "不合法" in r.json()["msg"]

    # 非法区县
    r = await client.post(
        "/api/user/addresses",
        headers=normal_headers,
        json=addr_payload(district="不存在区"),
    )
    assert r.json()["code"] == 1001


@pytest.mark.asyncio
async def test_order_requires_own_address(client, normal_headers, member_headers, goods):
    # 无地址 -> 业务错误，阻止支付
    r = await client.post(
        "/api/orders", headers=normal_headers, json={"goodsId": "g9", "qty": 1}
    )
    assert r.json()["code"] == 1002
    assert "收货地址" in r.json()["msg"]

    # 地址属于其他用户 -> 同样拦截
    r = await client.post(
        "/api/user/addresses", headers=member_headers, json=addr_payload(receiver="会员")
    )
    other_id = r.json()["data"]["id"]
    r = await client.post(
        "/api/orders",
        headers=normal_headers,
        json={"goodsId": "g9", "qty": 1, "addressId": other_id},
    )
    assert r.json()["code"] == 1002

    # 本人地址 -> 下单成功，订单快照收货信息
    r = await client.post("/api/user/addresses", headers=normal_headers, json=addr_payload())
    aid = r.json()["data"]["id"]
    r = await client.post(
        "/api/orders",
        headers=normal_headers,
        json={"goodsId": "g9", "qty": 2, "usePoints": 0, "addressId": aid},
    )
    assert r.json()["code"] == 0
    order_no = r.json()["data"]["orderNo"]

    r = await client.get(f"/api/user/orders/{order_no}", headers=normal_headers)
    order = r.json()["data"]
    assert order["receiver"] == "张三"
    assert order["receiverPhone"] == "13800138000"
    assert order["address"] == "浙江省杭州市西湖区文三路 100 号 1 幢 101"
