"""spots.py — 附近野塘列表 / 详情 / 评论"""

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.deps import get_current_user_id, get_db, get_optional_user_id
from ...core.response import fail, ok
from ...core.utils import gen_id, today
from ...models import Spot, SpotComment, User
from ...schemas.schemas import CommentIn, comment_out, spot_out
from ...services import ai, config_service, llm, sensitive

router = APIRouter(prefix="/spots", tags=["野塘"])

# 同一城市首次查询才需要调模型，加锁避免并发请求重复生成
_generate_lock = asyncio.Lock()

# 同一地区的钓点缓存有效期：超过这个天数，下次查询时重新调模型刷新
REFRESH_DAYS = 10


def _expired(spots: list[Spot]) -> bool:
    """该地区 AI 生成的钓点是否过期（超过 REFRESH_DAYS 天没更新）。

    管理员手工维护的钓点（added_by 非空）不参与过期判断：全为管理员钓点时
    不触发重新生成，避免覆盖人工内容。
    """
    stamps = [s.refreshed_at for s in spots if s.refreshed_at and not s.added_by]
    if not stamps:
        return False
    return datetime.now() - max(stamps) > timedelta(days=REFRESH_DAYS)


def _apply_cover(spot: Spot) -> None:
    """封面兜底规则：无实景图时取评论中的第一张图（与原型 api.js 一致）"""
    if spot.cover:
        spot.cover_from = spot.cover_from or "user"
        return
    for c in spot.comments:
        if c.images:
            spot.cover = c.images[0]
            spot.cover_from = "comment"
            return
    spot.cover_from = "none"


async def _spots_in_region(db: AsyncSession, region: str) -> list[Spot]:
    """该地区在线钓点（小程序侧只展示上架的）。"""
    rows = await db.scalars(
        select(Spot).where(
            Spot.region == region, or_(Spot.status.is_(None), Spot.status != "offline")
        )
    )
    return list(rows.all())


async def _generate_into_db(db: AsyncSession, region: str, place: str) -> list[Spot]:
    """生成（或到期后重新生成）该地区的钓点，失败时退回已有 / 全库数据。"""
    async with _generate_lock:
        exist = await _spots_in_region(db, region)   # 拿到锁后复查，避免并发重复生成
        if exist and not _expired(exist):
            return exist

        try:
            items = await ai.generate_spots(region, place)
        except llm.LLMError:
            if exist:
                return exist                          # 刷新失败就继续用旧数据
            rows = await db.scalars(select(Spot))
            return list(rows.all())                   # 首次就失败：兜底保证列表非空

        # 整批替换，避免新旧数据混在一起（id 由地区决定，替换后仍然稳定）。
        # 只替换 AI 生成的钓点，管理员手工新增/编辑过的（added_by 非空）保留。
        for old in exist:
            if not old.added_by:
                await db.delete(old)
        await db.flush()

        for item in items:
            comments = item.pop("comments")
            # cover / gallery 由模型返回并已校验，没有可用图片时为空 → 前端显示「暂无实景图」
            db.add(Spot(region=region, coord=None, **item))
            for c in comments:
                db.add(
                    SpotComment(
                        id=gen_id("c"),
                        spot_id=item["id"],
                        user_name=c["user"],
                        avatar=c["user"][:1],
                        rating=c["rating"],
                        text=c["text"],
                        date=today(),
                        images=[],
                    )
                )
        await db.commit()
        return await _spots_in_region(db, region)


@router.get("", summary="附近野塘（按所选位置由大模型生成并落库缓存）")
async def list_spots(
    keyword: str | None = None,
    sort: str = "distance",
    province: str | None = None,
    city: str | None = None,
    district: str | None = None,
    user_id: int | None = Depends(get_optional_user_id),
    db: AsyncSession = Depends(get_db),
):
    """传省/市/区则返回该城市的钓点（首次查询时由大模型生成并发入库）；不传则返回全部。"""
    if not (province or city or district) and user_id:
        user = await db.get(User, user_id)
        loc = (user.location or {}) if user else {}
        province, city, district = loc.get("province"), loc.get("city"), loc.get("district")

    place = " ".join([x for x in (province, city, district) if x])
    # 缓存维度精确到「区」：同一个城市的不同区，钓点各不相同
    region = "".join(x for x in (city, district) if x) or (province or "")

    if region:
        spots = await _spots_in_region(db, region)
        # 没数据，或数据已超过 REFRESH_DAYS 天没刷新 → 重新调模型生成
        if not spots or _expired(spots):
            spots = await _generate_into_db(db, region, place or region)
    else:
        rows = await db.scalars(
            select(Spot).where(or_(Spot.status.is_(None), Spot.status != "offline"))
        )
        spots = list(rows.all())

    spots = ai.rank_spots(spots, keyword)
    spots = ai.limit_by_distance(spots)
    for s in spots:
        _apply_cover(s)
    return ok([spot_out(s) for s in spots])


@router.get("/{spot_id}", summary="野塘详情")
async def spot_detail(
    spot_id: str,
    user_id: int | None = Depends(get_optional_user_id),
    db: AsyncSession = Depends(get_db),
):
    spot = await db.get(Spot, spot_id)
    if not spot:
        raise HTTPException(status_code=404, detail="野塘不存在")
    _apply_cover(spot)
    data = spot_out(spot, with_comments=True)
    if not data["gallery"]:
        data["gallery"] = [data["cover"]] if data["cover"] else []
    return ok(data)


@router.get("/{spot_id}/comments", summary="野塘评论列表")
async def list_comments(spot_id: str, db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(
        select(SpotComment)
        .where(SpotComment.spot_id == spot_id)
        .order_by(SpotComment.id.desc())
    )
    return ok([comment_out(c) for c in rows.all()])


@router.post("/{spot_id}/comments", summary="发布评论（需登录）")
async def post_comment(
    spot_id: str,
    payload: CommentIn,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    spot = await db.get(Spot, spot_id)
    if not spot:
        raise HTTPException(status_code=404, detail="野塘不存在")
    user = await db.get(User, user_id)

    # 敏感词前置拦截：命中则不落库，直接返回友好提示（词库可后台配置）
    extra = await config_service.get_str(db, "sensitive_words")
    hit = sensitive.find_sensitive(payload.text, extra)
    if hit:
        return fail(1001, f"评论包含敏感词「{hit}」，请修改后再发布")

    comment = SpotComment(
        id=gen_id("c"),
        spot_id=spot_id,
        user_id=user.id,
        user_name=user.nickname,
        avatar=user.avatar_text,
        rating=payload.rating,
        text=payload.text,
        date=today(),
        images=payload.images,
    )
    db.add(comment)

    spot.comment_count = (spot.comment_count or 0) + 1
    # 该塘此前没有实景封面时，用这条评论的图作为封面
    if not spot.cover and payload.images:
        spot.cover = payload.images[0]
        spot.cover_from = "comment"

    await db.commit()
    await db.refresh(comment)
    return ok(comment_out(comment))
