"""ai.py — 气象研判 / 野塘推荐的「大模型接口占位」

当前用确定性算法生成结果：同一地点 + 同一日期结果稳定，
保证前端联调时不会每次刷新都变，也便于写断言。
接真实大模型时，只需替换 build_weather / rank_spots 的内部实现，
函数签名与返回结构保持不变。

注意：seed_data.py 里是 snake_case（贴合 Python），
对外一律转成前端使用的 camelCase，转换在 camelize() 里统一完成。
"""

import hashlib
from copy import deepcopy

from ..core.utils import today
from ..seed_data import SLOTS, VERDICT_LEVELS, WEATHER
from . import llm, qweather, verdict


def _seed(*parts: str) -> int:
    raw = "|".join(str(p or "") for p in parts)
    return int(hashlib.md5(raw.encode("utf-8")).hexdigest(), 16)


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(x[:1].upper() + x[1:] for x in rest)


def camelize(obj):
    """递归把 dict 的 snake_case 键转成 camelCase"""
    if isinstance(obj, dict):
        return {_camel(k): camelize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [camelize(v) for v in obj]
    return obj


def verdict_of(score: float) -> dict:
    level = next((lv for lv in VERDICT_LEVELS if score >= lv["min"]), VERDICT_LEVELS[-1])
    return {
        "level": level["key"],
        "name": level["name"],
        "rank": level["rank"],
        "score": score,
        "advice": level["advice"],
    }


def _base_weather(place: str | None, place_detail: str | None, date_str: str | None) -> dict:
    """构造 snake_case 的天气观测基础数据（含按「地点+日期」的确定性抖动）。

    这是「兜底」路径：大模型未配置或调用失败时直接用本结果；
    大模型可用时，用它返回的实时数值覆盖本结果中对应的观测字段。
    """
    w = deepcopy(WEATHER)
    if place:
        w["place"] = place
    if place_detail:
        w["place_detail"] = place_detail

    # 确定性抖动：让不同点位 / 不同日期存在可复现的差异，避免全国都是一个杭州模板
    delta = (_seed(w["place"], date_str) % 13) - 6
    w["score"] = max(20, min(98, int(w["score"]) + delta))
    w["temp"] = w["temp"] + (delta // 3)
    w["water_temp"] = round(w["water_temp"] + delta / 10, 1)
    return w


def _build_slots(score: int) -> list:
    """按时段概率随总分等比缩放（最低 5、最高 99），返回 snake_case 时段列表。"""
    base = max(int(WEATHER["score"]), 1)
    slots = []
    for s in SLOTS:
        item = deepcopy(s)
        item["p"] = max(5, min(99, int(round(item["p"] * score / base))))
        slots.append(item)
    return slots


def _finalize(w: dict, date_str: str | None) -> dict:
    """把 snake_case 天气观测转成对外 camelCase 结构（weather + verdict + slots）。"""
    # 更新时间：查未来日期显示「预报」，今天显示「更新」
    parts = str(w.get("updated", "")).split()
    clock = parts[1] if len(parts) > 1 else "05:40"
    day = (date_str or today())[:10]
    w["updated"] = f"{day} {clock} " + ("更新" if day == today() else "预报")

    score = max(20, min(98, int(w.get("score") or 60)))
    w["score"] = score

    lv = verdict_of(score)
    v = {
        "level": lv["level"],
        "name": lv["name"],
        "rank": lv["rank"],
        "score": score,
        "advice": lv["advice"],
        "highlights": [
            f"气压 {w['pressure']}hPa（6h {w['pressure_6h']}）",
            f"{w['wind_dir']}{w['wind_level']} · 水体溶氧充足",
            f"水温 {w['water_temp']}℃ · 鱼活性区间",
            "日出后 1.5 小时为黄金窗口",
        ],
    }

    return camelize({"weather": w, "verdict": v, "slots": _build_slots(score)})


def build_weather(place: str | None, place_detail: str | None, date_str: str | None) -> dict:
    """返回 { weather, verdict, slots }（确定性兜底，保证离线/无大模型也能用）。"""
    return _finalize(_base_weather(place, place_detail, date_str), date_str)


# ---------------- 大模型生成天气观测 ----------------

# 真实天气数据由大模型生成时的系统提示词。
# 目标：输出字段与网页端（DeepSeek 等）天气报告一致 —— 气温范围、当前温度、AQI、
# 湿度、日出日落、风力（含「转」）等都要给到，且数值与地点/季节相符。
_WEATHER_SYSTEM_PROMPT = (
    "你是专业气象数据服务。根据给定的地点与日期，生成一份真实准确的当日天气观测/预报数据。\n"
    "只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块、不要多余文字。\n"
    "字段名必须严格使用以下英文下划线键，缺一不可：\n"
    "condition（天气现象，含趋势，如「阴转晴」）、condition_icon（sun/cloud/partly/rain 之一）、"
    "temp（当前温度，数字）、temp_max（最高温，数字）、temp_min（最低温，数字）、"
    "temp_feel（体感温度，数字）、water_temp（水温，数字，比气温低 0~3℃）、"
    "pressure（气压 hPa，数字）、pressure_6h（6 小时气压变化，带正负号，如「+3.2」）、"
    "pressure_trend（up/down/flat）、wind_dir（风向，如「西北风」）、"
    "wind_level（风力，如「4-5级转3-4级」，白天转夜间用「转」连接）、wind_speed（风速 m/s，数字）、"
    "humidity（相对湿度 %，整数）、cloud_cover（云量 %，整数）、rain_prob（降水概率 %，整数）、"
    "rainfall（降水量 mm，数字）、uv（紫外线指数，数字）、visibility（能见度 km，数字）、"
    "aqi（空气质量指数，数字）、aqi_text（对应 AQI 区间的等级：0-50优、51-100良、101-150轻度污染…）、"
    "moon_phase（月相，如「下弦月」）、moon_illum（月相照亮百分比，数字）、"
    "sunrise（日出 HH:MM，24 小时制补零）、sunset（日落 HH:MM，24 小时制补零）、"
    "golden_am（上午黄金钓鱼窗口，如「06:42-08:10」）、golden_pm（下午黄金钓鱼窗口）、"
    "solunar（潮汐/日月潮一句话描述）、score（鱼情综合评分，0-100 的整数，越高越适合钓鱼）。\n"
    "要求：所有数值必须真实合理、与「该地点、该季节、该日期」相符；每项都是明确字段值而非省略说明。"
)


_NUMERIC_WEATHER_KEYS = (
    "temp", "temp_max", "temp_min", "temp_feel", "water_temp",
    "pressure", "wind_speed", "humidity", "cloud_cover", "rain_prob",
    "rainfall", "uv", "visibility", "aqi", "moon_illum",
)

_STRING_WEATHER_KEYS = (
    "condition", "condition_icon", "pressure_6h", "pressure_trend",
    "wind_dir", "wind_level", "aqi_text", "moon_phase",
    "sunrise", "sunset", "golden_am", "golden_pm", "solunar",
)


def _as_number(value, default: float) -> float:
    """把大模型返回值强制转成数值；非法值回退默认（兜底不报错）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str) -> str:
    """把大模型返回值强制转成字符串；空值回退默认。"""
    s = str(value or "").strip()
    return s if s else default


def _merge_weather_obs(payload: dict, base: dict) -> dict:
    """把大模型返回的天气观测 JSON 合并到兜底数据上（只覆盖合法字段）。

    - 数值字段用 _as_number 转型（失败保留原值）；
    - 文本字段只接受非空字符串；
    - score 单独夹在 0~100 之间；
    - 缺失 / 非法的字段一律回落兜底，保证结构完整、接口不报错。
    """
    if not isinstance(payload, dict):
        return base
    w = deepcopy(base)

    if isinstance(payload.get("weather"), dict):
        obs = payload["weather"]
    else:
        obs = payload  # 允许模型直接返回平铺的天气观测对象

    for k in _NUMERIC_WEATHER_KEYS:
        if k in obs:
            w[k] = _as_number(obs[k], w.get(k))
    for k in _STRING_WEATHER_KEYS:
        if k in obs:
            w[k] = _as_str(obs[k], w.get(k))

    score_src = obs.get("score") if isinstance(obs, dict) else payload.get("score")
    if score_src is not None:
        w["score"] = int(max(0, min(100, _as_number(score_src, w.get("score")))))

    return w


async def generate_weather_traced(
    place: str | None, place_detail: str | None, date_str: str | None
) -> tuple[dict, str]:
    """按优先级生成天气数据，并返回实际数据来源（qweather / llm / fallback）。

    优先级：真实气象源（和风天气）→ 大模型生成 → 确定性兜底。
    """
    base = _base_weather(place, place_detail, date_str)
    source = "fallback"

    # 1) 优先：真实气象源（温度/湿度/气压/风/空气质量/日出日落等实测值）
    if qweather.enabled():
        try:
            obs = await qweather.fetch_weather(place or "")
            if obs:
                return _finalize(_merge_weather_obs(obs, base), date_str), "qweather"
        except qweather.QWeatherError:
            pass  # 天气源失败 → 降级到大模型

    # 2) 次选：大模型生成
    if llm.enabled():
        try:
            day_label = _day_label(date_str)
            raw = await llm.chat(
                [
                    {"role": "system", "content": _WEATHER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"地点：{place or '当前位置'}；详细位置：{place_detail or ''}；"
                                   f"日期：{(date_str or today())[:10]}（{day_label}）",
                    },
                ],
                max_tokens=1024,
                temperature=0.3,
                thinking=False,
            )
            base = _merge_weather_obs(llm.loads_json(raw), base)
            source = "llm"
        except llm.LLMError:
            pass  # 大模型失败时静默回退确定性数据

    # 3) 兜底
    return _finalize(base, date_str), source


async def generate_weather(place: str | None, place_detail: str | None, date_str: str | None) -> dict:
    """按优先级生成该地点+日期的天气数据，失败逐级降级，接口永不报错。

    优先级：真实气象源（和风天气）→ 大模型生成 → 确定性兜底。
    返回结构与 build_weather 完全一致：{ weather, verdict, slots }。
    """
    data, _ = await generate_weather_traced(place, place_detail, date_str)
    return data


def _day_label(date_str: str | None) -> str:
    """今天 / 明天 / 后天；更远日期回显 ISO，供大模型理解语境。"""
    import datetime as dt

    try:
        delta = (dt.date.fromisoformat((date_str or today())[:10]) - dt.date.fromisoformat(today())).days
    except ValueError:
        return (date_str or today())[:10]
    return {0: "今天", 1: "明天", 2: "后天"}.get(delta, (date_str or today())[:10])


def rank_spots(spots: list, keyword: str | None = None) -> list:
    """由近到远排序 + 关键词过滤。TODO: 接入大模型/地图源做真实推荐排序。"""
    items = list(spots)
    if keyword:
        k = keyword.strip()
        items = [
            s
            for s in items
            if k in (s.name or "")
            or k in "".join(s.fish or [])
            or k in (s.address or "")
        ]
    items.sort(key=lambda s: s.dist or 0)
    return items


# 10 公里内全给，10 公里外最多给 5 个
NEAR_KM = 10.0
FAR_LIMIT = 5


def limit_by_distance(spots: list, near_km: float = NEAR_KM, far_limit: int = FAR_LIMIT) -> list:
    """按距离截断：入参需已按距离升序，保证返回顺序不变。"""
    near = [s for s in spots if (s.dist or 0) <= near_km]
    far = [s for s in spots if (s.dist or 0) > near_km][:far_limit]
    return near + far


# ---------------- 大模型研判（鱼情评分） ----------------

# 同一「地点 + 日期」只问一次模型，避免重复计费与响应抖动；缓存结构化研判结论
_verdict_cache: dict[str, dict] = {}


async def judge_weather(data: dict, day_label: str = "今天") -> dict:
    """基于真实天气数据做鱼情智能评分（0-100），写入 verdict 并同步时段概率。

    - 大模型可用：结构化天气上下文注入 LLM，结合鱼类行为学规律推理；失败回退规则引擎；
    - 无论哪条路径，结果都包含：总分、4 维度分项得分与依据、适宜鱼种、总体依据、出钓建议；
    - verdict 的 level/name/rank 仍按确定性映射（verdict_of）从总分推导，保证与前端契约一致。
    """
    weather = data.get("weather") or {}
    place = weather.get("place")
    date_str = (weather.get("updated") or "")[:10]
    cache_key = f"{place}|{date_str}"

    cached = _verdict_cache.get(cache_key)
    if cached is None:
        judged = await verdict.judge(weather, place, date_str, day_label)
        cached = judged
        _verdict_cache[cache_key] = judged

    result = cached["result"]
    ai_generated = cached.get("aiGenerated", False)

    v = data.setdefault("verdict", {})
    lv = verdict_of(result["score"])
    v["score"] = result["score"]
    v["level"] = lv["level"]
    v["name"] = lv["name"]
    v["rank"] = lv["rank"]
    v["advice"] = result["advice"]
    v["dimensions"] = result["dimensions"]
    v["species"] = result["species"]
    v["reason"] = result["reason"]
    v["aiGenerated"] = ai_generated

    # 用最终得分重新缩放时段概率，保证「时段口型」与总分一致
    data["slots"] = camelize(_build_slots(result["score"]))
    data["aiGenerated"] = ai_generated
    return data


# ---------------- 大模型生成野塘 ----------------

_SPOT_SYSTEM_PROMPT = (
    "你是钓鱼钓点数据分析师。根据给定地区，生成 5 个该地区真实可信的野钓钓点"
    "（野塘 / 河湾 / 水库尾 / 城郊小塘）。只输出 JSON，不要解释、不要 markdown 代码块。\n"
    'JSON 结构：{"spots":[{"name":"塘名","address":"详细地址","fish":["鲫鱼","鲤鱼"],'
    '"pay":"免费","water":"水深 1.2-1.8 米","bottom":"淤泥底","crowd":"周末人多",'
    '"note":"一句话出钓提示","dist":1.2,"walk":"步行约 15 分钟","rating":4.6,'
    '"comments":[{"user":"钓友昵称","rating":5,"text":"一条真实感的评论"}]}]}\n'
    "要求：5 个钓点不重名，地址必须属于给定地区；dist 是距离用户的公里数，"
    "取 0.5~12 的一位小数；rating 取 4.0~5.0 的一位小数；每个钓点给 1~2 条 comments；"
    "pay 只能是「免费」「需登记」或「收费 X 元/天」这类短标签。"
)


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def spot_id(region: str, index: int) -> str:
    """同一地区的第 index 个钓点用固定 id，重复生成时可直接覆盖。"""
    return "ai" + hashlib.md5(f"{region}|{index}".encode("utf-8")).hexdigest()[:14]


async def generate_spots(region: str, place: str | None = None) -> list[dict]:
    """让大模型按地区生成野塘列表，返回清洗后的 dict（含 comments）。失败抛 LLMError。"""
    raw = await llm.chat(
        [
            {"role": "system", "content": _SPOT_SYSTEM_PROMPT},
            {"role": "user", "content": f"地区：{place or region}"},
        ],
        max_tokens=4096,
        temperature=0.8,
        # 结构化 JSON 生成不需要长篇思考，关掉思维链可显著缩短耗时
        thinking=False,
    )
    payload = llm.loads_json(raw)
    items = payload.get("spots") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise llm.LLMError("大模型未返回 spots 列表")

    spots: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 64)
        if not name:
            continue

        dist = _clamp(_num(item.get("dist"), 1.0), 0.1, 99.0)
        fish = [_text(x, 8) for x in (item.get("fish") or []) if _text(x, 8)][:6]

        comments = []
        for c in (item.get("comments") or [])[:3]:
            if not isinstance(c, dict):
                continue
            text = _text(c.get("text"), 200)
            if not text:
                continue
            comments.append({
                "user": _text(c.get("user"), 16) or "匿名钓友",
                "rating": int(_clamp(_num(c.get("rating"), 5), 1, 5)),
                "text": text,
            })

        spots.append({
            "id": spot_id(region, i),
            "name": name,
            "dist": round(dist, 1),
            "walk": _text(item.get("walk"), 32) or f"约 {round(dist, 1)} 公里",
            "rating": round(_clamp(_num(item.get("rating"), 4.5), 0.0, 5.0), 1),
            "comment_count": len(comments),
            "fish": fish or ["鲫鱼"],
            "pay": _text(item.get("pay"), 32) or "免费",
            # 大模型不出图：封面留空，前端显示「暂无实景图」，后续由评论区图片兜底
            "cover": None,
            "gallery": [],
            "address": _text(item.get("address"), 255),
            "water": _text(item.get("water"), 64),
            "bottom": _text(item.get("bottom"), 32),
            "crowd": _text(item.get("crowd"), 32),
            "note": _text(item.get("note"), 500),
            "comments": comments,
        })

    if not spots:
        raise llm.LLMError("大模型未生成有效钓点")
    return spots
