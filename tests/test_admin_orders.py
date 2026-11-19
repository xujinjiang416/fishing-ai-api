"""test_admin_orders.py — 管理后台订单管理全流程测试

覆盖：列表 / 快捷筛选 / 统计 / 详情 / 关闭 / 发货 / 改地址 / 备注异常 /
部分退款 / 全额退款（积分与库存回滚）/ 只读角色权限 / 导出。
"""

from datetime import datetime

import pytest
import pytest_asyncio

from app.core.security import create_admin_token, hash_password
from app.models import Admin, Goods, Order


@pytest_asyncio.fixture
async def order_data(session_factory, seeded):
    """在种子数据基础上补充商品与四种状态的订单。"""
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
    """删除订单：列表数量减少、详情 404、操作日志留痕、只读角色禁止。"""
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
    r = await client.get("/api/admin/orders")
    assert r.status_code == 401


# ---------------- 小程序端：订单详情 / 物流信息 ----------------
@pytest.mark.asyncio
async def test_user_order_detail_and_logistics(client, normal_headers, member_headers, order_data):
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
