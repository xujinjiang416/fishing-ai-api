"""tests/test_integration.py — 集成测试（走 HTTP + 真实路由 + SQLite 数据库）

覆盖需求 3 / 4 / 5 / 6 / 7：
- 签到积分（会员 5 / 非会员 1，无前置操作）；
- 公开配置接口 /api/config；
- 后台积分配置修改实时生效 + 操作日志；
- 评论列表 / 单条删除 / 批量删除 + 级联维护评论数 + 操作日志；
- 敏感词前置拦截；
- 会员套餐读取与开通。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_integration.py`。
文件内为 async 函数，未显式加 @pytest.mark.asyncio，依赖 pytest.ini 的 asyncio_mode = auto。

前置条件与依赖：全部依赖 conftest.py 的 client / seeded 及三种身份鉴权头；
seeded 已提供管理员、月卡套餐、普通 / 会员用户、钓点 ai_test1 与两条评论、默认配置。

注意事项：
- 每个用例开始前 seeded 已清空配置服务内存缓存，避免 15 秒 TTL 影响「实时生效」断言；
- 评论删除会级联更新钓点的 commentCount，断言通过公开钓点详情接口观察。
"""


async def test_public_config_returns_rules_and_plans(client):
    """公开配置接口返回签到积分、广告积分与上架套餐。

    前置条件：seeded 已写入 CONFIG_DEFS 默认值与上架月卡。
    预期行为：code=0，签到会员 / 非会员分别为 5 / 1，广告单次 5 分，
    plans 中包含 id=month 的套餐。
    断言意图：确认小程序无需登录即可拿到规则与套餐。
    """
    r = await client.get("/api/config")
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["checkin"]["memberPoints"] == 5
    assert data["checkin"]["normalPoints"] == 1
    assert data["ad"]["points"] == 5
    assert any(p["id"] == "month" for p in data["plans"])


async def test_checkin_points_by_role(client, normal_headers, member_headers):
    """非会员签到 +1 分、会员签到 +5 分（均无需看广告）。

    前置条件：普通用户 id=1、会员用户 id=2，当日均未签到。
    预期行为：两种身份首次签到分别返回 1 / 5 分；普通用户当天再次签到
    返回业务码 1001（重复签到被拦截）。
    断言意图：验证按会员身份区分积分，且签到按自然日去重。
    """
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
    """后台修改签到积分配置后：接口实时生效，并记录操作日志。

    前置条件：管理员已登录，配置为默认值（非会员签到 1 分、广告 5 分）。
    预期行为：PUT 后返回新值；公开配置接口同步为新值；
    非会员此时签到得 7 分；操作日志首条 action 为 config.update。
    断言意图：确认配置改动无需重启即可生效且可审计。
    """
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
    """未携带管理员 token 访问后台接口应 401。

    前置条件：请求不带任何鉴权头。
    预期行为：GET /api/admin/comments 返回 HTTP 401。
    断言意图：确认后台路由统一挂载管理员鉴权依赖。
    """
    r = await client.get("/api/admin/comments")
    assert r.status_code == 401


async def test_comments_list_delete_and_cascade(client, admin_headers):
    """评论列表分页 + 单条删除 + 钓点评论数级联维护 + 操作日志。

    前置条件：钓点 ai_test1 有评论 c_1 / c_2，钓点 comment_count=2。
    预期行为：列表 total=2 且含用户 / 钓点 / 正文；删除 c_1 后 total=1，
    钓点详情的 commentCount 降为 1；action=comment.delete 的日志恰有 1 条。
    断言意图：确认删除评论时评论数与审计日志同步维护。
    """
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
    """批量删除评论。

    前置条件：钓点 ai_test1 下有 c_1 / c_2 两条评论。
    预期行为：批量删除返回 deleted=2，再次查询列表 total=0。
    断言意图：确认批量接口返回实际删除条数并真实落库。
    """
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
    """评论命中敏感词时被前置拦截（不落库）。

    前置条件：普通用户已登录，钓点 ai_test1 存在。
    预期行为：含「加微信」的评论返回业务码 1001（发布被拒）；
    正常评论返回 code=0 可成功发布。
    断言意图：确认拦截发生在写库之前，且不误伤正常内容。
    """
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
    """会员状态接口返回后台配置套餐；开通后会员状态生效。

    前置条件：普通用户为非会员，seeded 提供上架月卡（30 天 / 39.9 元）。
    预期行为：开通前 isMember=False，plans 首项为 month；
    购买月卡成功并返回 memberExpire；再次查询 isMember=True。
    断言意图：验证套餐读取来自后台配置且开通立即改变会员态。
    """
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
    """购买不存在的套餐应被拒绝。

    前置条件：普通用户已登录，库中仅有 month 套餐。
    预期行为：plan=no_such 返回业务码 1001。
    断言意图：防止非法套餐 id 写入会员记录。
    """
    r = await client.post("/api/user/membership", headers=normal_headers, json={"plan": "no_such"})
    assert r.json()["code"] == 1001


async def test_membership_stats(client, admin_headers, normal_headers):
    """会员购买统计接口可用（初始 0 单，购买后 +1）。

    前置条件：管理员已登录；初始无会员购买记录。
    预期行为：购买前 totalOrders=0；普通用户购买月卡后 totalOrders=1、
    totalAmount=39.9，byPlan 首项名称为「月卡」。
    断言意图：确认统计随购买实时累计且按套餐聚合。
    """
    before = await client.get("/api/admin/membership-stats", headers=admin_headers)
    assert before.json()["code"] == 0
    assert before.json()["data"]["totalOrders"] == 0

    await client.post("/api/user/membership", headers=normal_headers, json={"plan": "month"})

    after = await client.get("/api/admin/membership-stats", headers=admin_headers)
    assert after.json()["data"]["totalOrders"] == 1
    assert after.json()["data"]["totalAmount"] == 39.9
    assert after.json()["data"]["byPlan"][0]["name"] == "月卡"
