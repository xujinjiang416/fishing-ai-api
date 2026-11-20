"""test_admin_orders.py — 管理后台订单管理全流程测试

覆盖：列表 / 快捷筛选 / 统计 / 详情 / 关闭 / 发货 / 改地址 / 备注异常 /
部分退款 / 全额退款（积分与库存回滚）/ 只读角色权限 / 导出。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_admin_orders.py`，
依赖 conftest.py 的 client / admin_headers / seeded 夹具。

注意事项：
- order_data 夹具构造了四种订单状态，用例之间依赖其固定初始值；
  同一用例内的多次请求会共享同一个测试库，因此顺序敏感的操作（关闭 / 发货 / 退款）
  都选择互不冲突的订单号；
- 金额单位为元，积分抵扣按 150 积分 = 1 元折算（由业务层处理）。
"""

from datetime import datetime

import pytest
import pytest_asyncio

from app.core.security import create_admin_token, hash_password
from app.models import Admin, Goods, Order


@pytest_asyncio.fixture
async def order_data(session_factory, seeded):
    """在种子数据基础上补充商品与四种状态的订单。

    作用域：function（依赖 seeded）。写入内容：
    - 商品 g1（测试钓竿，100 元，库存 10，上架）；
    - 订单 T1001 待支付 / T1002 待发货（均属普通用户 id=1，各抵扣 150 积分）；
    - 订单 T1003 已发货（会员 id=2，数量 2，顺丰 SF0001）/ T1004 已完成（自提）；
    - 只读角色管理员 id=2（observer / secret，角色「观察员」）。
    """
    async with session_factory() as db:
        db.add(
            Goods(
                id="g1",
                name="测试钓竿",
                cat="鱼竿",
                price=100.0,
                orig=120.0,
                img="/static/img/product-rod.jpg",
                sales="0",
                tags=["测试"],
                spec="4.5m",
                stock=10,
                desc="测试商品",
                status="on",
            )
        )
        db.add_all(
            [
                Order(
                    order_no="T1001",
                    user_id=1,
                    goods_id="g1",
                    goods_name="测试钓竿",
                    qty=1,
                    total=100,
                    use_points=150,
                    pay_amount=99,
                    status="待支付",
                    pay_method="微信支付",
                    delivery_type="快递",
                    receiver="普通用户",
                    receiver_phone="13900000001",
                    address="浙江省杭州市西湖区 1 号",
                    created_at=datetime(2026, 11, 18, 11, 0),
                ),
                Order(
                    order_no="T1002",
                    user_id=1,
                    goods_id="g1",
                    goods_name="测试钓竿",
                    qty=1,
                    total=100,
                    use_points=150,
                    pay_amount=99,
                    status="待发货",
                    pay_method="微信支付",
                    delivery_type="快递",
                    receiver="普通用户",
                    receiver_phone="13900000001",
                    address="浙江省杭州市西湖区 1 号",
                    paid_at=datetime(2026, 11, 18, 10, 5),
                    created_at=datetime(2026, 11, 18, 10, 0),
                ),
                Order(
                    order_no="T1003",
                    user_id=2,
                    goods_id="g1",
                    goods_name="测试钓竿",
                    qty=2,
                    total=200,
                    use_points=0,
                    pay_amount=200,
                    status="已发货",
                    pay_method="微信支付",
                    delivery_type="快递",
                    receiver="会员用户",
                    receiver_phone="13900000002",
                    address="浙江省杭州市西湖区 2 号",
                    logistics_company="顺丰速运",
                    logistics_no="SF0001",
                    paid_at=datetime(2026, 11, 17, 9, 0),
                    shipped_at=datetime(2026, 11, 17, 15, 0),
                    created_at=datetime(2026, 11, 17, 9, 0),
                ),
                Order(
                    order_no="T1004",
                    user_id=2,
                    goods_id="g1",
                    goods_name="测试钓竿",
                    qty=1,
                    total=100,
                    use_points=0,
                    pay_amount=100,
                    status="已完成",
                    pay_method="微信支付",
                    delivery_type="自提",
                    created_at=datetime(2026, 11, 16, 9, 0),
                ),
            ]
        )
        # 只读角色管理员（id=2）
        db.add(
            Admin(
                id=2,
                username="observer",
                password_hash=hash_password("secret"),
                name="观察员",
                role="观察员",
                avatar_text="观",
            )
        )
        await db.commit()
    yield


@pytest.mark.asyncio
async def test_order_list_stats_and_filters(client, admin_headers, order_data):
    """覆盖订单列表分页、快捷筛选、关键词 / 状态筛选与统计概览。

    前置条件：order_data 提供 4 笔订单（T1001~T1004），管理员已登录。
    预期行为：
    - 列表 total=4 且按下单时间倒序（首条 T1001）；
    - quick=unshipped 只返回待发货订单；
    - userKeyword / keyword / status（逗号多选）分别命中对应子集；
    - 统计接口返回总数、各状态计数、待处理数（待支付 + 待发货）与 7 天趋势。
    断言意图：确认后台筛选与看板统计口径一致。
    """
    r = await client.get("/api/admin/orders", headers=admin_headers, params={"page": 1, "pageSize": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 4
    # 按下单时间倒序
    assert body["data"]["list"][0]["no"] == "T1001"

    # 快捷筛选：待发货
    r = await client.get("/api/admin/orders", headers=admin_headers, params={"quick": "unshipped"})
    assert [x["no"] for x in r.json()["data"]["list"]] == ["T1002"]

    # 用户关键词
    r = await client.get(
        "/api/admin/orders", headers=admin_headers, params={"userKeyword": "会员", "pageSize": 20}
    )
    assert {x["no"] for x in r.json()["data"]["list"]} == {"T1003", "T1004"}

    # 订单号 / 商品关键词
    r = await client.get("/api/admin/orders", headers=admin_headers, params={"keyword": "T1003"})
    assert [x["no"] for x in r.json()["data"]["list"]] == ["T1003"]

    # 状态多选（逗号分隔）
    r = await client.get(
        "/api/admin/orders", headers=admin_headers, params={"status": "待支付,已发货", "pageSize": 20}
    )
    assert {x["no"] for x in r.json()["data"]["list"]} == {"T1001", "T1003"}

    # 统计概览
    r = await client.get("/api/admin/orders/stats", headers=admin_headers)
    data = r.json()["data"]
    assert data["totalOrders"] == 4
    assert data["counts"]["unpaid"] == 1
    assert data["counts"]["unshipped"] == 1
    assert data["pendingCount"] == 2
    assert len(data["trend"]["days"]) == 7


@pytest.mark.asyncio
async def test_order_detail_contains_full_chain(client, admin_headers, order_data):
    """覆盖订单详情聚合内容与不存在订单的处理。

    前置条件：T1003 为会员订单（数量 2、顺丰 SF0001）。
    预期行为：详情返回订单号、用户信息、商品明细（subtotal = 单价 × 数量）、物流单号，
    logs 为列表（允许为空）；不存在的订单号返回 404。
    断言意图：保证详情页可一次性拿到用户 / 商品 / 物流 / 日志四块数据。
    """
    r = await client.get("/api/admin/orders/T1003", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["no"] == "T1003"
    assert data["userInfo"]["nickname"] == "会员用户"
    assert data["goodsDetail"]["name"] == "测试钓竿"
    assert data["goodsDetail"]["subtotal"] == 200
    assert data["logisticsNo"] == "SF0001"
    assert data["logs"] == [] or isinstance(data["logs"], list)

    r = await client.get("/api/admin/orders/NOT_EXIST", headers=admin_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_close_order_flow(client, admin_headers, order_data):
    """覆盖关闭未支付订单及重复关闭的保护。

    前置条件：T1001 为待支付订单。
    预期行为：首次关闭成功，状态变为「已关闭」并写入 closedAt；
    重复关闭返回业务码 1001；详情日志中存在 close 动作且操作人为当前管理员。
    断言意图：确认状态机不可逆、操作可追溯。
    """
    r = await client.post("/api/admin/orders/T1001/close", headers=admin_headers)
    assert r.json()["code"] == 0
    assert r.json()["data"]["status"] == "已关闭"
    assert r.json()["data"]["closedAt"]

    # 重复关闭返回业务错误
    r = await client.post("/api/admin/orders/T1001/close", headers=admin_headers)
    assert r.json()["code"] == 1001

    # 操作日志已记录
    r = await client.get("/api/admin/orders/T1001", headers=admin_headers)
    logs = r.json()["data"]["logs"]
    assert any(l["action"] == "close" and l["operator"] == "系统管理员" for l in logs)


@pytest.mark.asyncio
async def test_ship_and_complete_flow(client, admin_headers, order_data):
    """覆盖发货与确认完成的状态流转。

    前置条件：T1002 为待发货订单。
    预期行为：发货成功后状态「已发货」并回填物流公司 / 单号 / shippedAt；
    非待发货状态再次发货返回 1001；随后确认完成，状态「已完成」并写入 completedAt。
    断言意图：验证只允许待发货 -> 已发货 -> 已完成的正向流转。
    """
    r = await client.post(
        "/api/admin/orders/T1002/ship",
        headers=admin_headers,
        json={"logisticsCompany": "中通快递", "logisticsNo": "ZT1234567890"},
    )
    data = r.json()["data"]
    assert data["status"] == "已发货"
    assert data["logisticsCompany"] == "中通快递"
    assert data["shippedAt"]

    # 非待发货订单不能再次发货
    r = await client.post(
        "/api/admin/orders/T1002/ship",
        headers=admin_headers,
        json={"logisticsCompany": "中通快递", "logisticsNo": "ZT2"},
    )
    assert r.json()["code"] == 1001

    # 发货 → 完成
    r = await client.post("/api/admin/orders/T1002/complete", headers=admin_headers)
    assert r.json()["data"]["status"] == "已完成"
    assert r.json()["data"]["completedAt"]


@pytest.mark.asyncio
async def test_address_and_remark(client, admin_headers, order_data):
    """覆盖后台修改收货地址、订单备注 / 异常标记与快捷筛选。

    前置条件：T1003 已发货、T1004 已完成。
    预期行为：已发货订单可改收货人 / 手机号 / 地址；已完成订单改地址返回 1001；
    备注与异常标记写入后在详情回显，且 quick=abnormal 可命中该订单。
    断言意图：确认售后场景下的地址变更受限规则与异常单可被快速检索。
    """
    r = await client.put(
        "/api/admin/orders/T1003/address",
        headers=admin_headers,
        json={"receiver": "新收货人", "receiverPhone": "13700000000", "address": "浙江省宁波市海曙区 9 号"},
    )
    data = r.json()["data"]
    assert data["receiver"] == "新收货人"
    assert data["address"].startswith("浙江省宁波市")

    # 已完成订单不允许修改地址
    r = await client.put(
        "/api/admin/orders/T1004/address",
        headers=admin_headers,
        json={"receiver": "x", "receiverPhone": "13700000000", "address": "somewhere"},
    )
    assert r.json()["code"] == 1001

    r = await client.post(
        "/api/admin/orders/T1003/remark",
        headers=admin_headers,
        json={"remark": "用户要求改约周六送达", "abnormal": True},
    )
    data = r.json()["data"]
    assert data["remark"] == "用户要求改约周六送达"
    assert data["abnormal"] is True

    # 异常快捷筛选可以命中
    r = await client.get("/api/admin/orders", headers=admin_headers, params={"quick": "abnormal"})
    assert [x["no"] for x in r.json()["data"]["list"]] == ["T1003"]


@pytest.mark.asyncio
async def test_refund_partial_then_full(client, admin_headers, order_data):
    """覆盖部分退款、全额退款与驳回退款三种处理结果。

    前置条件：T1002 待发货、T1003 已发货（200 元）、T1004 已完成。
    预期行为：部分退款 20 元 -> 状态「部分退款」、refundStatus=approved；
    全额退款 200 元 -> 状态「已退款」，详情 refundAmount 同步；
    驳回退款 -> refundStatus=rejected 且订单保持「已完成」。
    断言意图：验证退款金额与状态的一致性。
    """
    # 部分退款
    r = await client.post(
        "/api/admin/orders/T1002/refund",
        headers=admin_headers,
        json={"amount": 20, "reason": "商品有轻微瑕疵", "approve": True},
    )
    data = r.json()["data"]
    assert data["status"] == "部分退款"
    assert data["refundAmount"] == 20
    assert data["refundStatus"] == "approved"

    # 已发货订单全额退款：积分回滚 + 库存恢复
    r = await client.post(
        "/api/admin/orders/T1003/refund",
        headers=admin_headers,
        json={"amount": 200, "reason": "整单退款", "approve": True},
    )
    data = r.json()["data"]
    assert data["status"] == "已退款"

    # 校验详情中的退款金额
    r = await client.get("/api/admin/orders/T1003", headers=admin_headers)
    assert r.json()["data"]["refundAmount"] == 200

    # 驳回退款
    r = await client.post(
        "/api/admin/orders/T1004/refund",
        headers=admin_headers,
        json={"amount": 0, "reason": "不符合退款条件", "approve": False},
    )
    assert r.json()["data"]["refundStatus"] == "rejected"
    assert r.json()["data"]["status"] == "已完成"


@pytest.mark.asyncio
async def test_full_refund_restores_points_and_stock(
    client, admin_headers, order_data, session_factory
):
    """覆盖整单退款后的库存恢复、积分返还与积分流水。

    前置条件：T1002 使用 150 积分抵扣，商品 g1 初始库存 10。
    预期行为：退款后商品库存 +1（10 -> 11），用户积分 = 退款前 + 150，
    且 points_logs 中存在包含「退款」字样的流水。
    断言意图：确认退款与积分 / 库存账务一致，且留下可审计的流水。
    """
    from sqlalchemy import select

    from app.models import User

    async with session_factory() as db:
        goods = await db.get(Goods, "g1")
        assert goods.stock == 10
        user = await db.get(User, 1)
        points_before = user.points

    r = await client.post(
        "/api/admin/orders/T1002/refund",
        headers=admin_headers,
        json={"amount": 99, "reason": "整单退款", "approve": True},
    )
    assert r.json()["data"]["status"] == "已退款"

    async with session_factory() as db:
        goods = await db.get(Goods, "g1")
        user = await db.get(User, 1)
        assert goods.stock == 11
        assert user.points == points_before + 150
        # 积分流水存在
        from app.models import PointsLog

        logs = (
            await db.scalars(select(PointsLog).where(PointsLog.user_id == 1))
        ).all()
        assert any("退款" in (l.reason or "") for l in logs)


@pytest.mark.asyncio
async def test_readonly_role_cannot_operate(client, order_data):
    """覆盖只读角色（观察员）的权限边界。

    前置条件：管理员 id=2 角色为「观察员」。
    预期行为：列表等读接口返回 code=0；关闭订单等写操作返回 HTTP 403 / code=403。
    断言意图：确认角色级权限校验落在操作接口而非仅前端隐藏。
    """
    headers = {"Authorization": "Bearer " + create_admin_token(2)}
    # 只读角色可查看
    r = await client.get("/api/admin/orders", headers=headers)
    assert r.json()["code"] == 0

    # 但不能执行操作
    r = await client.post("/api/admin/orders/T1001/close", headers=headers)
    assert r.status_code == 403
    assert r.json()["code"] == 403


@pytest.mark.asyncio
async def test_export_selected_fields(client, admin_headers, order_data):
    """覆盖订单导出 CSV 的字段选择与编码。

    前置条件：T1002 为待发货订单（普通用户，实付 99 元）。
    预期行为：响应为 text/csv，内容以 UTF-8 BOM 开头（Excel 兼容），
    表头按 fields 参数映射为中文列名，且包含命中订单的关键字段。
    断言意图：确认导出字段可配置、中文不乱码。
    """
    r = await client.get(
        "/api/admin/orders/export",
        headers=admin_headers,
        params={"fields": "no,user,amount", "quick": "unshipped"},
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8")
    assert text.startswith("\ufeff")
    assert text.lstrip("\ufeff").splitlines()[0] == "订单编号,下单用户,实付金额"
    assert "T1002" in text and "普通用户" in text
    assert "99" in text


@pytest.mark.asyncio
async def test_delete_order(client, admin_headers, order_data):
    """删除订单：列表数量减少、详情 404、操作日志留痕、只读角色禁止。

    前置条件：T1004 已完成（不影响其它状态的断言）。
    预期行为：删除后 total 由 4 变 3 且详情 404；全局日志出现 order.delete 记录，
    target 为订单号且 detail 含「删除订单」；只读管理员删除返回 403。
    断言意图：确认删除后的列表 / 详情可见性一致，并保留审计痕迹。
    """
    r = await client.delete("/api/admin/orders/T1004", headers=admin_headers)
    assert r.json()["code"] == 0

    r = await client.get("/api/admin/orders/T1004", headers=admin_headers)
    assert r.status_code == 404

    r = await client.get("/api/admin/orders", headers=admin_headers)
    assert r.json()["data"]["total"] == 3
    assert "T1004" not in {x["no"] for x in r.json()["data"]["list"]}

    # 全局操作日志留痕
    r = await client.get(
        "/api/admin/logs", headers=admin_headers, params={"action": "order.delete", "pageSize": 10}
    )
    logs = r.json()["data"]["list"]
    assert logs and logs[0]["target"] == "T1004"
    assert "删除订单" in (logs[0]["detail"] or "")

    # 只读角色不可删除
    readonly = {"Authorization": "Bearer " + create_admin_token(2)}
    r = await client.delete("/api/admin/orders/T1001", headers=readonly)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_orders_require_admin(client, order_data):
    """未登录访问后台订单接口应返回 401。

    前置条件：不携带任何鉴权头。
    预期行为：GET /api/admin/orders 返回 HTTP 401。
    断言意图：确认后台接口默认拒绝匿名访问。
    """
    r = await client.get("/api/admin/orders")
    assert r.status_code == 401


# ---------------- 小程序端：订单详情 / 物流信息 ----------------
@pytest.mark.asyncio
async def test_user_order_detail_and_logistics(client, normal_headers, member_headers, order_data):
    """覆盖小程序端订单列表字段、详情、越权隔离与物流信息。

    前置条件：普通用户拥有 T1001 / T1002，会员用户拥有 T1003 / T1004。
    预期行为：
    - 列表返回商品名、图片、数量等展示字段，普通用户可见 2 笔；
    - 详情返回实付金额、状态、收货人；他人订单返回 404；
    - 已发货订单物流含公司 / 单号 / 官方电话，未发货订单三者均为空字符串；
    - 未登录访问详情返回 401。
    断言意图：保证小程序订单页所需字段完整且严格按用户隔离。
    """
    # 列表返回商品图片 / 规格 / 数量等展示字段
    r = await client.get("/api/user/orders", headers=normal_headers)
    data = r.json()["data"]
    assert len(data) == 2
    item = [x for x in data if x["no"] == "T1002"][0]
    assert item["goods"] == "测试钓竿"
    assert item["goodsImg"] == "/static/img/product-rod.jpg"
    assert item["qty"] == 1

    # 详情
    r = await client.get("/api/user/orders/T1002", headers=normal_headers)
    detail = r.json()["data"]
    assert detail["amount"] == 99
    assert detail["status"] == "待发货"
    assert detail["receiver"] == "普通用户"

    # 他人的订单不可见
    r = await client.get("/api/user/orders/T1003", headers=normal_headers)
    assert r.status_code == 404

    # 已发货订单：物流公司 / 单号 / 官方电话
    r = await client.get("/api/user/orders/T1003/logistics", headers=member_headers)
    lg = r.json()["data"]
    assert lg["company"] == "顺丰速运"
    assert lg["no"] == "SF0001"
    assert lg["phone"] == "95338"

    # 未发货订单：物流字段为空（前端展示「暂无物流信息」）
    r = await client.get("/api/user/orders/T1002/logistics", headers=normal_headers)
    lg = r.json()["data"]
    assert lg["company"] == "" and lg["no"] == "" and lg["phone"] == ""

    # 未登录不可访问
    r = await client.get("/api/user/orders/T1002")
    assert r.status_code == 401
