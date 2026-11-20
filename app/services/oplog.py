"""oplog.py — 管理后台操作日志写入工具

统一封装操作日志的落库，保证「配置变更 / 评论删除」等关键动作都有迹可循。
调用方负责 `await db.commit()`，本模块只负责 `db.add(...)`，以便与业务写入同事务。

职责：
- 集中定义操作动作常量，避免各路由散落魔法字符串；
- 统一构造 OperationLog 记录（管理员 / 动作 / 对象 / 详情 / IP）。

关键数据流：
  路由调用 add_log(...) → db.add(log) → 调用方 commit 时一并落库

依赖：数据库 operation_logs 表（SQLAlchemy AsyncSession）。

失败降级策略：
- 本模块不捕获异常、不做提交，写入失败由调用方的业务事务统一回滚；
- admin 为空时按「系统管理员」记录，保证系统任务也有迹可循。

注意事项：
- 不要在 add_log 内 commit，否则会破坏调用方的原子性；
- detail 建议使用人类可读文本，便于后台审计。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import OperationLog

# 动作类型常量：集中定义，避免各路由散落魔法字符串
ACTION_CONFIG_UPDATE = "config.update"          # 修改积分配置
ACTION_COMMENT_DELETE = "comment.delete"        # 删除单条评论
ACTION_COMMENT_BATCH_DELETE = "comment.batch_delete"  # 批量删除评论
ACTION_CACHE_REFRESH = "cache.refresh"          # 刷新单条天气缓存
ACTION_CACHE_BATCH_REFRESH = "cache.batch_refresh"  # 批量刷新天气缓存
ACTION_CACHE_DELETE = "cache.delete"            # 删除单条天气缓存
ACTION_CACHE_BATCH_DELETE = "cache.batch_delete"  # 批量删除天气缓存
ACTION_CACHE_RULE_UPDATE = "cache.rule.update"  # 修改缓存过期规则


def add_log(
    db: AsyncSession,
    *,
    action: str,
    admin=None,
    target: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> OperationLog:
    """构造一条操作日志并加入会话（不提交）。

    参数：
    - action：动作类型（见上方常量）；
    - admin：操作管理员对象（取 id / name），可为空（系统任务）；
    - target：操作对象标识（配置键 / 评论 id 等）；
    - detail：人类可读的变更详情；
    - ip：来源 IP。

    Returns:
        OperationLog: 已加入会话、尚未提交的日志对象。

    Raises:
        无：仅构造对象，异常由调用方事务处理。
    """
    log = OperationLog(
        admin_id=getattr(admin, "id", None),
        admin_name=getattr(admin, "name", None) or "系统管理员",
        action=action,
        target=target,
        detail=detail,
        ip=ip,
    )
    db.add(log)
    return log
