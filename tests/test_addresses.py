"""test_addresses.py — 收货地址管理与支付前地址校验

覆盖：新增（首条自动默认）/ 编辑 / 设为默认 / 删除默认后自动顺延 /
越权隔离 / 字段校验 / 下单必须携带本人地址并快照到订单。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_addresses.py`，
依赖 conftest.py 提供的 client / headers 夹具（pytest.ini 已开启 asyncio_mode = auto）。

注意事项：
- 默认地址的置顶与顺延逻辑由接口内部维护，用例只通过 API 观察结果；
- 本文件的 geo / goods 夹具在 seeded 基础上追加数据，均为 function 作用域，互不影响。
"""

import pytest
import pytest_asyncio

from app.models import City, District, Goods, Province


@pytest_asyncio.fixture
async def geo(session_factory, seeded):
    """最小行政区划数据，用于校验省市区合法性。

    作用域：function（依赖 seeded）。写入「浙江省 -> 杭州市 -> 西湖区」一条链路，
    仅覆盖地址接口合法性校验所需的最小集合，不写入其它省市。
    """
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
    """上架测试商品 g9，供下单链路使用。

    作用域：function（依赖 seeded）。价格 50 元、库存 10，status="on" 保证可下单。
    """
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
    """构造新增 / 编辑地址的请求体（浙江省杭州市西湖区，默认手机号 13800138000）。

    Args:
        **overrides: 需要覆盖的字段，如 receiver / phone / district / isDefault。

    Returns:
        dict: 可直接作为 JSON 请求体的地址字典。
    """
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
    """覆盖地址增删改查与默认地址维护的完整链路。

    前置条件：普通用户已登录（normal_headers），初始无地址。
    预期行为：
    - 第一条地址自动成为默认，fullAddress 由省市区 + 详细地址拼接；
    - 第二条默认关闭，列表把默认地址置顶；
    - 编辑可改收货人 / 详细地址；设为默认会取消原默认；
    - 删除当前默认后，剩余第一条自动顺延为默认。
    断言意图：全部通过接口响应观察，确保前端无需自行维护默认标记。
    """
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
    """覆盖字段校验、越权隔离与未登录拦截。

    前置条件：普通用户与会员用户均已存在（seeded），普通用户后续创建一条地址。
    预期行为：
    - 手机号非法或必填字段为空白 -> HTTP 422（请求体校验失败）；
    - 其他用户读取 / 修改 / 删除该地址 -> 404（不暴露资源存在性）；
    - 未携带 token 访问地址列表 -> 401。
    断言意图：确认鉴权先于业务、越权一律按「不存在」处理。
    """
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
    """覆盖省 / 市 / 区县合法性校验。

    前置条件：geo 夹具写入「浙江省 - 杭州市 - 西湖区」。
    预期行为：合法组合返回 code=0；不存在省份 / 区县返回业务码 1001，
    错误信息包含「不合法」提示。
    断言意图：防止脏地址进入订单快照。
    """
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
    """覆盖「下单必须携带本人地址」的支付前校验与订单快照。

    前置条件：goods 夹具提供可售商品 g9（50 元 / 库存 10）；普通用户初始无地址。
    预期行为：
    - 未传 addressId -> 业务码 1002，提示缺少收货地址；
    - 传入他人地址 -> 同样 1002（不能借用他人地址下单）；
    - 传入本人地址 -> 下单成功，订单详情中的收货人 / 手机号 / 地址为地址簿快照。
    断言意图：保证订单收货信息不可被越权或缺失。
    """
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
