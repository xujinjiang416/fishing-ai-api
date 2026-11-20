"""rewards.py — 每日签到 / 观看广告赚积分

规则（对应需求 1、3）：
- 签到：会员与非会员都「无需任何前置操作」直接签到；
  会员每次 +N 分、非会员每次 +M 分（N/M 由后台积分配置决定，默认 5 / 1）；
  已移除历史上「看广告后完成签到」的全部逻辑与「连续 7 天额外奖励」。
- 广告：观看广告仅用于「赚取积分」，不再用于解锁任何内容；
  单次积分与每日额度同样由后台积分配置决定，实现后台改配置即时生效。

鉴权要求：三个接口均需用户登录（Authorization: Bearer <user token>），无管理员接口。

依赖服务：MySQL（users / checkin_records / ad_rewards 表）、services.config_service
（积分配置，带 15 秒内存缓存）、user.add_points（统一积分变动与流水入口）。

注意事项：积分值全部实时读取后台配置；同一自然日重复签到返回业务错误而非 500；
日期口径统一取 core.utils.today()（本地时区日期字符串）。
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_current_user_id, get_db
from ...core.response import fail, ok
from ...core.utils import today, today_cn
from ...models import AdReward, CheckinRecord, User
from ...services import config_service
from .user import add_points

router = APIRouter(tags=["签到与广告"])

# 连续签到日历展示用的星期标签（纯展示，不参与积分计算）
CHECKIN_DAYS = ["一", "二", "三", "四", "五", "六", "日"]


@router.get("/checkin", summary="签到状态")
async def checkin_status(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """查询今日签到状态。

    返回今日签到状态、连续天数与积分规则（积分值实时取自后台配置）。
    方法与路径：GET /checkin（需登录）。
    响应：checkedToday 今日是否已签到、streak 当前连续天数、maxStreak 历史最长、
    isMember 是否会员、dailyPoints 当前身份对应积分、memberPoints / normalPoints 两档规则、
    days 星期标签、date 展示用中文日期。
    说明：纯读接口，不写入任何数据；配置缺失时由 config_service 回退默认值（5 / 1）。
    """
    user = await db.get(User, user_id)
    member_points = await config_service.get_int(db, "checkin_member_points")
    normal_points = await config_service.get_int(db, "checkin_normal_points")
    return ok(
        {
            "checkedToday": user.last_checkin_date == today(),
            "streak": user.streak,
            "maxStreak": user.max_streak,
            # 当前身份对应的签到积分；同时把会员/非会员两个值都带回，便于前端展示规则
            "isMember": bool(user.is_member),
            "dailyPoints": member_points if user.is_member else normal_points,
            "memberPoints": member_points,
            "normalPoints": normal_points,
            "days": CHECKIN_DAYS,
            "date": today_cn(),
        }
    )


@router.post("/checkin", summary="每日签到（会员 / 非会员均无需看广告）")
async def do_checkin(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """执行每日签到。

    方法与路径：POST /checkin（需登录）。
    幂等：同一自然日重复调用返回 fail(1001, "今日已签到")，不会重复发放积分。
    连续天数：上次签到为昨天则 +1，否则重置为 1，并同步更新历史最长连续天数。
    积分：按身份读取 checkin_member_points / checkin_normal_points（默认 5 / 1），
    经 add_points 统一入账并写积分流水。
    响应：本次积分、连续天数、当前余额、checkedToday；签到记录的 bonus 恒为 0
    （已取消连续 7 天额外奖励）。
    """
    user = await db.get(User, user_id)
    if user.last_checkin_date == today():
        return fail(1001, "今日已签到", {"checkedToday": True, "streak": user.streak})

    # 昨天签过 -> 连续天数 +1，否则从 1 重新开始
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    streak = user.streak + 1 if user.last_checkin_date == yesterday else 1

    # 积分值按身份从后台配置读取（会员 / 非会员分开配置，默认 5 / 1）
    key = "checkin_member_points" if user.is_member else "checkin_normal_points"
    base = await config_service.get_int(db, key)

    user.streak = streak
    user.max_streak = max(user.max_streak, streak)
    user.last_checkin_date = today()
    await add_points(db, user, base, "每日签到")

    # 签到记录落库（bonus 恒为 0：已取消「连续 7 天额外奖励」）
    db.add(CheckinRecord(user_id=user.id, date=today(), points=base, streak=streak, bonus=0))
    await db.commit()

    return ok(
        {
            "points": base,
            "base": base,
            "bonus": 0,
            "streak": streak,
            "isMember": bool(user.is_member),
            "pointsBalance": user.points,
            "checkedToday": True,
        }
    )


@router.post("/ads/reward", summary="看完广告领积分（客户端播放成功后调用）")
async def ad_reward(
    user_id: int = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    """观看广告领取积分。

    方法与路径：POST /ads/reward（需登录），客户端需在广告播放成功后调用。
    频次控制：跨天（ads_date != 今天）自动清零当日计数；当日次数达到后台配置的
    会员 / 非会员额度后返回 fail(1001)，额度 ≤ 0 视为不可观看。
    积分：ad_points 实时读取后台配置；累加 ads_watched_today / ad_points_today，
    并通过 add_points 入账、写入 AdReward 记录。
    响应：本次积分、今日已看次数、额度上限、当前余额。
    注意：广告仅用于「赚取积分」，不再承担任何「解锁内容」的职责。
    """
    user = await db.get(User, user_id)

    # 跨天重置当日广告计数
    if user.ads_date != today():
        user.ads_date = today()
        user.ads_watched_today = 0
        user.ad_points_today = 0

    # 积分与额度：会员 / 非会员分别配置
    ad_points = await config_service.get_int(db, "ad_points")
    limit = await config_service.get_int(
        db, "ad_limit_member" if user.is_member else "ad_limit_normal"
    )
    if limit <= 0 or user.ads_watched_today >= limit:
        return fail(1001, "今日广告额度已用完", {"todayCount": user.ads_watched_today, "limit": limit})

    user.ads_watched_today += 1
    user.ad_points_today += ad_points
    await add_points(db, user, ad_points, "观看广告")

    db.add(AdReward(user_id=user.id, date=today(), points=ad_points))
    await db.commit()

    return ok(
        {
            "points": ad_points,
            "todayCount": user.ads_watched_today,
            "limit": limit,
            "pointsBalance": user.points,
        }
    )
