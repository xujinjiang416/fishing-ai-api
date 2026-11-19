"""tests/test_integration.py — 集成测试（走 HTTP + 真实路由 + SQLite 数据库）

覆盖需求 3 / 4 / 5 / 6 / 7：
- 签到积分（会员 5 / 非会员 1，无前置操作）；
- 公开配置接口 /api/config；
- 后台积分配置修改实时生效 + 操作日志；
- 评论列表 / 单条删除 / 批量删除 + 级联维护评论数 + 操作日志；
- 敏感词前置拦截；
- 会员套餐读取与开通。
"""


async def test_public_config_returns_rules_and_plans(client):
    """公开配置接口返回签到积分、广告积分与上架套餐。"""
    r = await client.get("/api/config")
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["checkin"]["memberPoints"] == 5
    assert data["checkin"]["normalPoints"] == 1
    assert data["ad"]["points"] == 5
    assert any(p["id"] == "month" for p in data["plans"])


async def test_checkin_points_by_role(client, normal_headers, member_headers):
    """非会员签到 +1 分、会员签到 +5 分（均无需看广告）。"""
    r1 = await client.post("/api/checkin", headers=normal_headers)
    assert r1.json()["code"] == 0
    assert r1.json()["data"]["points"] == 1

    r2 = await client.post("/api/checkin", headers=member_headers)
    assert r2.json()["code"] == 0
    assert r2.json()["data"]["points"] == 5

    # 同日重复签到应被拦截
    r3 = await client.post("/api/checkin", headers=normal_headers)
    assert r3.json()["code"] == 1001


async def test_admin_config_update_takes_effect_and_logs(client, admin_headers, normal_headers):
    """后台修改签到积分配置后：接口实时生效，并记录操作日志。"""
    r = await client.put(
        "/api/admin/config",
        headers=admin_headers,
        json={"checkinNormalPoints": 7, "adPoints": 9},
    )
    assert r.json()["code"] == 0
    assert r.json()["data"]["checkinNormalPoints"] == 7
    assert r.json()["data"]["adPoints"] == 9

    # 公开接口同步生效
    pub = await client.get("/api/config")
    assert pub.json()["data"]["checkin"]["normalPoints"] == 7
    assert pub.json()["data"]["ad"]["points"] == 9

    # 非会员签到现在应得 7 分（实时生效）
    ck = await client.post("/api/checkin", headers=normal_headers)
    assert ck.json()["data"]["points"] == 7

    # 操作日志已记录
    logs = await client.get("/api/admin/logs", headers=admin_headers)
    assert logs.json()["data"]["total"] >= 1
    assert logs.json()["data"]["list"][0]["action"] == "config.update"


async def test_admin_requires_auth(client):
    """未携带管理员 token 访问后台接口应 401。"""
    r = await client.get("/api/admin/comments")
    assert r.status_code == 401


async def test_comments_list_delete_and_cascade(client, admin_headers):
    """评论列表分页 + 单条删除 + 钓点评论数级联维护 + 操作日志。"""
    r = await client.get("/api/admin/comments", headers=admin_headers, params={"page": 1, "pageSize": 10})
    data = r.json()["data"]
    assert data["total"] == 2
    first = data["list"][0]
    assert first["user"] and first["spot"] == "测试野塘" and first["text"]

    # 单条删除
    d = await client.delete("/api/admin/comments/c_1", headers=admin_headers)
    assert d.json()["code"] == 0

    after = await client.get("/api/admin/comments", headers=admin_headers)
    assert after.json()["data"]["total"] == 1

    # 钓点评论数级联减少
    spot = await client.get("/api/spots/ai_test1")
    assert spot.json()["data"]["commentCount"] == 1

    # 删除操作写入日志
    logs = await client.get("/api/admin/logs", headers=admin_headers, params={"action": "comment.delete"})
    assert logs.json()["data"]["total"] == 1


async def test_comments_batch_delete(client, admin_headers):
    """批量删除评论。"""
    r = await client.post(
        "/api/admin/comments/batch-delete",
        headers=admin_headers,
        json={"ids": ["c_1", "c_2"]},
    )
    assert r.json()["code"] == 0
    assert r.json()["data"]["deleted"] == 2

    after = await client.get("/api/admin/comments", headers=admin_headers)
    assert after.json()["data"]["total"] == 0


async def test_sensitive_comment_blocked(client, normal_headers):
    """评论命中敏感词时被前置拦截（不落库）。"""
    r = await client.post(
        "/api/spots/ai_test1/comments",
        headers=normal_headers,
        json={"text": "加微信详聊，价格好商量", "rating": 5, "images": []},
    )
    assert r.json()["code"] == 1001

    # 正常评论可以发布
    ok = await client.post(
        "/api/spots/ai_test1/comments",
        headers=normal_headers,
        json={"text": "今天鱼口不错，连杆了", "rating": 5, "images": []},
    )
    assert ok.json()["code"] == 0


async def test_membership_plans_and_purchase(client, normal_headers):
    """会员状态接口返回后台配置套餐；开通后会员状态生效。"""
    r = await client.get("/api/user/membership", headers=normal_headers)
    data = r.json()["data"]
    assert data["isMember"] is False
    assert data["plans"] and data["plans"][0]["id"] == "month"

    buy = await client.post("/api/user/membership", headers=normal_headers, json={"plan": "month"})
    assert buy.json()["code"] == 0
    assert buy.json()["data"]["memberExpire"]

    r2 = await client.get("/api/user/membership", headers=normal_headers)
    assert r2.json()["data"]["isMember"] is True


async def test_membership_invalid_plan(client, normal_headers):
    """购买不存在的套餐应被拒绝。"""
    r = await client.post("/api/user/membership", headers=normal_headers, json={"plan": "no_such"})
    assert r.json()["code"] == 1001


async def test_membership_stats(client, admin_headers, normal_headers):
    """会员购买统计接口可用（初始 0 单，购买后 +1）。"""
    before = await client.get("/api/admin/membership-stats", headers=admin_headers)
    assert before.json()["code"] == 0
    assert before.json()["data"]["totalOrders"] == 0

    await client.post("/api/user/membership", headers=normal_headers, json={"plan": "month"})

    after = await client.get("/api/admin/membership-stats", headers=admin_headers)
    assert after.json()["data"]["totalOrders"] == 1
    assert after.json()["data"]["totalAmount"] == 39.9
    assert after.json()["data"]["byPlan"][0]["name"] == "月卡"
