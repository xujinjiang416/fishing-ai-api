"""tests/test_units.py — 单元测试

覆盖：积分配置服务（默认值 / 兜底 / 变更明细）与敏感词校验。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_units.py`；
异步用例依赖 pytest.ini 的 asyncio_mode = auto，无需显式标记。

前置条件与注意事项：
- 配置服务带 15 秒进程内缓存，涉及读取配置的用例在开头调用 clear_cache() 隔离；
- 使用 session_factory 直连测试库，不经过 HTTP 层，属于服务层单元测试；
- 敏感词用例为纯函数调用，不依赖数据库。
"""

from app.models import AppConfig
from app.services import config_service, sensitive


async def test_config_defaults_and_fallback(session_factory):
    """配置缺失时读取默认值；非法值回退默认；未知键返回 0。

    前置条件：app_configs 表为空（仅使用 session_factory，未加载 seeded 的数据）。
    预期行为：签到会员 / 非会员 / 广告积分读取 CONFIG_DEFS 默认值 5 / 1 / 5；
    未登记键返回 0；写入非法值「abc」后读取仍回退默认 5 而不抛异常。
    断言意图：保证配置缺失或脏数据不会导致业务报错。
    """
    config_service.clear_cache()
    async with session_factory() as db:
        # 表为空 → 读取 CONFIG_DEFS 默认值
        assert await config_service.get_int(db, "checkin_member_points") == 5
        assert await config_service.get_int(db, "checkin_normal_points") == 1
        assert await config_service.get_int(db, "ad_points") == 5
        # 未登记的键 → 0（不抛异常）
        assert await config_service.get_int(db, "not_exist_key") == 0

        # 写入一个非法值，读取时应回退默认值而不是报错
        db.add(AppConfig(key="ad_points", value="abc", remark=""))
        await db.commit()
        config_service.clear_cache()
        assert await config_service.get_int(db, "ad_points") == 5


async def test_config_apply_updates_returns_changes(session_factory):
    """apply_updates 返回 [(key, old, new)] 变更明细，且无变化时不产生记录。

    前置条件：空配置表，默认非会员签到积分为 1。
    预期行为：改为 3 时返回 [("checkin_normal_points", "1", "3")] 并生效；
    再次提交相同值返回空列表。
    断言意图：保证操作日志只记录真实变更，避免噪音。
    """
    config_service.clear_cache()
    async with session_factory() as db:
        changes = await config_service.apply_updates(db, {"checkin_normal_points": 3})
        await db.commit()
        assert changes == [("checkin_normal_points", "1", "3")]
        assert await config_service.get_int(db, "checkin_normal_points") == 3

        # 相同值再次提交 → 无变更
        changes2 = await config_service.apply_updates(db, {"checkin_normal_points": 3})
        await db.commit()
        assert changes2 == []


async def test_snapshot_shape(session_factory):
    """snapshot 输出结构包含 pointsPerYuan / checkin / ad / plans。

    前置条件：空配置表（使用默认值），未写入套餐。
    预期行为：pointsPerYuan=150，会员签到 5 分，会员每日广告上限 20，
    plans 始终为列表（无套餐时为空列表）。
    断言意图：固化前端消费的字段形状，避免缺字段导致渲染异常。
    """
    config_service.clear_cache()
    async with session_factory() as db:
        snap = await config_service.snapshot(db)
        assert snap["pointsPerYuan"] == 150
        assert snap["checkin"]["memberPoints"] == 5
        assert snap["ad"]["limitMember"] == 20
        assert isinstance(snap["plans"], list)


def test_sensitive_builtin_words():
    """内置词库命中 / 未命中。

    前置条件：不传自定义词库，仅使用系统内置词表。
    预期行为：命中「加微信」返回该词，正常出钓文案返回 None。
    断言意图：确认内置词库可独立生效且不误判。
    """
    assert sensitive.find_sensitive("加微信详聊") == "加微信"
    assert sensitive.find_sensitive("今天天气不错，适合出钓") is None


def test_sensitive_custom_words():
    """自定义词库与内置词库合并判断。

    前置条件：传入自定义词库字符串「代购,刷单」。
    预期行为：分别命中「代购」「刷单」；空文本返回 None 且不抛异常。
    断言意图：确认后台配置的扩展词能与内置词共同拦截。
    """
    assert sensitive.find_sensitive("限量代购", "代购,刷单") == "代购"
    assert sensitive.find_sensitive("刷单返现", "代购,刷单") == "刷单"
    # 空文本不报错
    assert sensitive.find_sensitive("") is None
