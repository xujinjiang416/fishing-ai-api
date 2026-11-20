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

from datetime import datetime, timedelta

from app.models import Plan, User


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


async def test_register_and_login_with_username(client):
    """用户名 + 密码注册与登录全流程。

    前置条件：库中无同名用户。
    预期行为：注册成功返回 token 与用户信息；同一用户名可立即登录；
    重复注册返回 400；错误密码登录返回 400。
    断言意图：确认手机号已不再是登录账号，用户名成为唯一登录标识。
    """
    payload = {
        "username": "angler_01",
        "password": "abc12345",
        "confirmPassword": "abc12345",
    }
    r = await client.post("/api/auth/register", json=payload)
    assert r.json()["code"] == 0
    assert r.json()["data"]["token"]
    user = r.json()["data"]["user"]
    # 用户名与昵称完全一致，不再生成「钓友+后缀」
    assert user["username"] == "angler_01"
    assert user["nickname"] == "angler_01"

    login = await client.post(
        "/api/auth/login", json={"account": "angler_01", "password": "abc12345"}
    )
    assert login.json()["code"] == 0
    assert login.json()["data"]["token"]

    dup = await client.post("/api/auth/register", json=payload)
    assert dup.status_code == 400
    assert dup.json()["code"] == 400
    assert dup.json()["msg"] == "用户名已存在"

    wrong = await client.post(
        "/api/auth/login", json={"account": "angler_01", "password": "wrong123"}
    )
    assert wrong.status_code == 400


async def test_auth_rejects_invalid_username_and_password(client):
    """用户名 / 密码边界：长度越界、纯空格、含空格、含表情均返回 422。

    断言意图：验证后端二次校验可拦截绕过前端的所有非法输入。
    """
    base = {"password": "abc12345", "confirmPassword": "abc12345"}

    # 用户名 3 位（过短）与 17 位（过长）
    for name in ("abc", "a" * 17):
        r = await client.post("/api/auth/register", json={**base, "username": name})
        assert r.status_code == 422
        assert "4-16" in r.json()["msg"]

    # 纯空格 / 含空格 / 含表情
    for name, keyword in (
        ("      ", "空格"),
        ("abc def", "空格"),
        ("abc😀def", "表情"),
    ):
        r = await client.post("/api/auth/register", json={**base, "username": name})
        assert r.status_code == 422
        assert keyword in r.json()["msg"]

    # 密码纯空格 / 含表情 / 过短
    for pwd, keyword in (
        ("      ", "纯空格"),
        ("abc😀123", "表情"),
        ("12345", "6-16"),
    ):
        r = await client.post(
            "/api/auth/register",
            json={"username": "angler_02", "password": pwd, "confirmPassword": pwd},
        )
        assert r.status_code == 422
        assert keyword in r.json()["msg"]

    # 登录入参同样走二次校验
    r = await client.post("/api/auth/login", json={"account": "abc😀def", "password": "abc12345"})
    assert r.status_code == 422


async def test_profile_hides_phone_and_preserves_data(client, normal_headers, session_factory):
    """个人资料响应不返回手机号，仅返回用户名与绑定状态；数据库手机号完整保留。

    前置条件：普通用户已有用户名，模拟历史手机号 13812345678。
    预期行为：
    - GET /user/profile 返回 username 与 phoneBound，且不含 phone 字段；
    - users.phone 仍为原值；
    - PUT /user/profile 传入 phone（预留绑定入口）可写入，但响应不回传手机号；
    - 登录返回的用户对象同样不含手机号。
    断言意图：满足手机号不对外展示，同时保留绑定扩展能力与原始数据。
    """
    async with session_factory() as db:
        u = await db.get(User, 1)
        u.phone = "13812345678"
        await db.commit()

    r = await client.get("/api/user/profile", headers=normal_headers)
    data = r.json()["data"]
    assert data["username"] == "13900000001"
    assert data["phoneBound"] is True
    assert "phone" not in data

    async with session_factory() as db:
        assert (await db.get(User, 1)).phone == "13812345678"

    # 预留绑定入口：可写入手机号，但响应仍不暴露该字段
    r2 = await client.put(
        "/api/user/profile", headers=normal_headers, json={"phone": "13900001111"}
    )
    assert r2.json()["code"] == 0
    assert "phone" not in r2.json()["data"]
    assert r2.json()["data"]["phoneBound"] is True

    async with session_factory() as db:
        assert (await db.get(User, 1)).phone == "13900001111"

    login = await client.post(
        "/api/auth/login", json={"account": "13900000001", "password": "123456"}
    )
    assert login.json()["code"] == 0
    assert "phone" not in login.json()["data"]["user"]
    assert login.json()["data"]["user"]["username"] == "13900000001"

    # 历史账号：用户名与手机号相同时，username 置空，避免变相展示手机号
    async with session_factory() as db:
        u = await db.get(User, 1)
        u.account = "13900001111"
        await db.commit()
    r3 = await client.get("/api/user/profile", headers=normal_headers)
    assert r3.json()["data"]["username"] == ""
    assert r3.json()["data"]["phoneBound"] is True


async def test_admin_users_returns_username(client, admin_headers, session_factory):
    """管理后台用户列表返回并支持按用户名检索。

    前置条件：普通用户设置独立用户名 angler_norm 与手机号（二者不同）。
    预期行为：keyword 命中用户名，列表项 username 与账号一致。
    断言意图：确认管理后台展示与登录账号规则保持同步。
    """
    async with session_factory() as db:
        u = await db.get(User, 1)
        u.account = "angler_norm"
        u.phone = "13812345678"
        await db.commit()

    r = await client.get("/api/admin/users?keyword=angler_norm", headers=admin_headers)
    data = r.json()["data"]
    assert data["total"] >= 1
    row = next(x for x in data["list"] if x["uid"] == "u_norm")
    assert row["username"] == "angler_norm"
    assert row["nickname"] == "普通用户"


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


async def test_admin_plan_sort_persists_and_orders(client, admin_headers, session_factory):
    """后台套餐排序权重：可更新持久化，列表按权重降序、同权重按创建时间降序。

    前置条件：seeded 提供上架月卡（sort=1）。
    预期行为：
    - PUT /admin/plans/month {"sort": 9} 成功且返回 sort=9；
    - 再插入权重 9（更晚创建）与权重 5 的套餐；
    - GET /admin/plans 依次为「同权重更晚创建」「月卡」「权重 5」；
    - 公开配置接口返回的套餐顺序与后台一致。
    断言意图：验证排序字段的接收、持久化与默认排序规则。
    """
    r = await client.put("/api/admin/plans/month", headers=admin_headers, json={"sort": 9})
    assert r.json()["code"] == 0
    assert r.json()["data"]["sort"] == 9

    base = datetime.now()
    async with session_factory() as db:
        db.add_all(
            [
                Plan(
                    id="p_tie",
                    name="同权重套餐",
                    duration=7,
                    duration_label="7 天",
                    price=9.9,
                    status="on",
                    sort=9,
                    created_at=base + timedelta(seconds=1),
                ),
                Plan(
                    id="p_low",
                    name="低权重套餐",
                    duration=3,
                    duration_label="3 天",
                    price=3.3,
                    status="on",
                    sort=5,
                    created_at=base + timedelta(seconds=2),
                ),
            ]
        )
        await db.commit()

    rows = (await client.get("/api/admin/plans", headers=admin_headers)).json()["data"]
    assert [p["id"] for p in rows][:3] == ["p_tie", "month", "p_low"]

    public = (await client.get("/api/config")).json()["data"]["plans"]
    assert [p["id"] for p in public][:3] == ["p_tie", "month", "p_low"]

    # 再次读取确认权重已持久化
    rows2 = (await client.get("/api/admin/plans", headers=admin_headers)).json()["data"]
    assert next(p for p in rows2 if p["id"] == "month")["sort"] == 9


async def test_admin_plan_sort_rejects_negative(client, admin_headers):
    """排序权重必须为非负整数，负数应被 422 拒绝且不写库。

    断言意图：校验后端对排序权重的合法性约束。
    """
    r = await client.put("/api/admin/plans/month", headers=admin_headers, json={"sort": -1})
    assert r.status_code == 422

    rows = (await client.get("/api/admin/plans", headers=admin_headers)).json()["data"]
    assert next(p for p in rows if p["id"] == "month")["sort"] == 1


async def test_admin_plan_create_defaults_sort_zero(client, admin_headers):
    """新增套餐未传排序权重时默认写入 0。

    断言意图：确认默认值与前端表单默认值一致。
    """
    r = await client.post(
        "/api/admin/plans",
        headers=admin_headers,
        json={"name": "体验卡", "duration": 1, "price": 1.0},
    )
    assert r.json()["code"] == 0
    assert r.json()["data"]["sort"] == 0


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
