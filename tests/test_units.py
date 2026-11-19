"""tests/test_units.py — 单元测试

覆盖：积分配置服务（默认值 / 兜底 / 变更明细）与敏感词校验。
"""

from app.models import AppConfig
from app.services import config_service, sensitive


async def test_config_defaults_and_fallback(session_factory):
    """配置缺失时读取默认值；非法值回退默认；未知键返回 0。"""
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
    """apply_updates 返回 [(key, old, new)] 变更明细，且无变化时不产生记录。"""
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
    """snapshot 输出结构包含 pointsPerYuan / checkin / ad / plans。"""
    config_service.clear_cache()
    async with session_factory() as db:
        snap = await config_service.snapshot(db)
        assert snap["pointsPerYuan"] == 150
        assert snap["checkin"]["memberPoints"] == 5
        assert snap["ad"]["limitMember"] == 20
        assert isinstance(snap["plans"], list)


def test_sensitive_builtin_words():
    """内置词库命中 / 未命中。"""
    assert sensitive.find_sensitive("加微信详聊") == "加微信"
    assert sensitive.find_sensitive("今天天气不错，适合出钓") is None


def test_sensitive_custom_words():
    """自定义词库与内置词库合并判断。"""
    assert sensitive.find_sensitive("限量代购", "代购,刷单") == "代购"
    assert sensitive.find_sensitive("刷单返现", "代购,刷单") == "刷单"
    # 空文本不报错
    assert sensitive.find_sensitive("") is None
