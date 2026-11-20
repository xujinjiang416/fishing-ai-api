"""verdict.py — 基于真实天气数据的鱼情评分与智能研判

核心目标（需求 2/3/4）：
- 把多维度真实天气（气温/气压/风/降水/湿度/日照等）标准化成结构化上下文；
- 用「大模型 + 鱼类行为学规则」双路径，输出 0-100 分的可解释鱼情结论；
- 结论包含：总分、4 维度分项得分与依据、适宜鱼种推荐、总体依据、出钓建议。

评分维度体系（权重和为 1.0）：
  摄食意愿 30% · 游动活跃度 25% · 咬口频率 30% · 鱼种适配度 15%

双路径（保证「可解释、可验证、可测试」）：
  1. 大模型可用 → 结构化天气上下文注入 LLM，结合行为学规律推理并输出 JSON；
  2. 大模型不可用/失败 → 走确定性规则引擎 rule_verdict()，同样给出分项得分与依据。

说明：本模块消费 camelCase 天气字段（与 ai._finalize 的对外结构一致）。

职责：
- 把天气观测标准化为大模型可理解的上下文（build_brief）；
- 规则引擎与 LLM 双路径输出同构的 0-100 分结论；
- 对 LLM 返回做规范化与兜底，保证字段完整。

关键数据流：
  weather(camelCase) → build_brief → llm.chat → loads_json
                     → normalize（缺失字段回退 rule_verdict）
                     → { score, dimensions, species, reason, advice }

依赖：
- app.services.llm（OpenAI 兼容接口）：可选，未配置时只用规则引擎。

失败降级策略：
- llm 未启用 / 调用失败 / JSON 非法：直接返回规则引擎结果（aiGenerated=False）；
- LLM 缺字段 / 字段非法：逐项回退规则引擎结果，保证结构完整。

注意事项：
- 权重和固定为 1.0，score 应与分项加权和基本一致；
- 规则引擎是「可测试基线」，修改阈值需同步关注前端展示口径。
"""

import json
from datetime import datetime

from . import llm

# ---- 评分维度与权重（权重和为 1.0） ----
SCORE_DIMENSIONS = (
    {"key": "feeding", "name": "摄食意愿", "weight": 0.30},
    {"key": "activity", "name": "游动活跃度", "weight": 0.25},
    {"key": "bite", "name": "咬口频率", "weight": 0.30},
    {"key": "species", "name": "鱼种适配度", "weight": 0.15},
)

# 维度 key -> 维度定义（含中文名与权重），供规范化时查表
_DIM_MAP = {d["key"]: d for d in SCORE_DIMENSIONS}


def _num(value, default: float = 0.0) -> float:
    """安全转 float；失败回退 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: int = 0, hi: int = 100) -> int:
    """四舍五入取整并夹取到 [lo, hi]（默认 0~100）。"""
    return max(lo, min(hi, int(round(v))))


# 已下线的水温表述：大模型可能凭经验自行补充，这里统一清洗为气温口径。
_WATER_TERMS = ("水体温度", "水层温度", "水温")


def _scrub_water_terms(text: str) -> str:
    """把文本中的水温表述替换为气温，确保输出不再出现水温相关内容。

    Args:
        text: 大模型返回的依据 / 建议文本。

    Returns:
        str: 清洗后的文本。
    """
    s = str(text or "")
    for term in _WATER_TERMS:
        s = s.replace(term, "气温")
    return s


def _season(date_str: str | None) -> str:
    """据日期推断季节（用于鱼种/食性适配）。

    Args:
        date_str: ISO 日期字符串，可为空。

    Returns:
        str: 春/夏/秋/冬；解析失败时按当前月份推断。
    """
    try:
        m = datetime.fromisoformat((date_str or "")[:10]).month
    except ValueError:
        m = datetime.now().month
    if m in (3, 4, 5):
        return "春季"
    if m in (6, 7, 8):
        return "夏季"
    if m in (9, 10, 11):
        return "秋季"
    return "冬季"


def _sunshine_hours(sunrise: str, sunset: str) -> str:
    """由日出/日落（HH:MM）计算日照时长，作为光照维度。

    Args:
        sunrise: 日出时间（HH:MM）。
        sunset: 日落时间（HH:MM）。

    Returns:
        str: 形如 ``12.3 小时``；解析失败返回「未知」。
    """
    try:
        fmt = "%H:%M"
        a = datetime.strptime(str(sunrise).strip(), fmt)
        b = datetime.strptime(str(sunset).strip(), fmt)
        mins = (b - a).total_seconds() / 60
        if mins < 0:
            mins += 24 * 60
        return f"{round(mins / 60, 1)} 小时"
    except (ValueError, AttributeError):
        return "未知"


# ---------------- 规则引擎（确定性兜底 + 校准基线） ----------------

def _feeding_score(w: dict) -> tuple[int, str]:
    """摄食意愿：气温主驱动，气压趋势与湿度修正。

    分步说明：
    1. 气温缺失按 78 分；
    2. 按气温落在 15~25 / 10~15 或 25~30 / 5~10 或 30~35 / 极端 四档给基础分；
    3. 气压上升 +6、下降 -12；湿度 >95 或 <20 再 -10；
    4. 结果夹取到 0~100，并拼接可读依据。

    Args:
        w: camelCase 天气观测（读取 temp/pressureTrend/humidity）。

    Returns:
        tuple[int, str]: (摄食意愿得分, 依据文本)。
    """
    t = w.get("temp")
    if t is None:
        s, reasons = 78, ["气温数据缺失，按中性处理"]
    else:
        if 15 <= t <= 25:
            s, band = 88, "处于鲫鲤摄食最适区间"
        elif 10 <= t < 15 or 25 < t <= 30:
            s, band = 66, "略偏离最适区间，摄食尚可"
        elif 5 <= t < 10 or 30 < t <= 35:
            s, band = 42, "气温偏高/偏低，摄食减弱"
        else:
            s, band = 22, "气温极端，摄食意愿低"
        reasons = [f"气温{t:.0f}℃{band}"]

    if w.get("pressureTrend") == "up":
        s += 6
        reasons.append("气压回升促进开口")
    elif w.get("pressureTrend") == "down":
        s -= 12
        reasons.append("气压下降抑制摄食")
    hum = w.get("humidity")
    if hum is not None and (hum > 95 or hum < 20):
        s -= 10
        reasons.append("湿度极端不利")
    return _clamp(s), "；".join(reasons)


def _activity_score(w: dict) -> tuple[int, str]:
    """游动活跃度：气温舒适度 + 风力/紫外线修正。

    分步说明：
    1. 气温缺失按 78 分；10~24℃ 给 88 分，边缘区间 64 分，极端 35 分；
    2. 风速 >8m/s -20、>5m/s -8、其余 +3；
    3. 紫外线 >9 再 -6；
    4. 结果夹取到 0~100。

    Args:
        w: camelCase 天气观测（读取 temp/windSpeed/uv）。

    Returns:
        tuple[int, str]: (活跃度得分, 依据文本)。
    """
    t = w.get("temp")
    if t is None:
        s, band = 78, "气温适中"
    elif 10 <= t <= 24:
        s, band = 88, "气温舒适，鱼群活跃"
    elif 4 <= t < 10 or 24 < t <= 30:
        s, band = 64, "气温略偏，活跃度一般"
    else:
        s, band = 35, "气温极端，鱼群趋静"

    reasons = [band]
    ws = w.get("windSpeed")
    if ws is not None:
        if ws > 8:
            s -= 20
            reasons.append("大风浪扰动明显")
        elif ws > 5:
            s -= 8
            reasons.append("风力偏大")
        else:
            s += 3
            reasons.append("风力适中利于游动")
    if _num(w.get("uv"), 5) > 9:
        s -= 6
        reasons.append("强紫外线下鱼趋深水")
    return _clamp(s), "；".join(reasons)


def _bite_score(w: dict) -> tuple[int, str]:
    """咬口频率：气压趋势 + 风力 + 温差。

    分步说明：
    1. 以 75 为基准分；气压上升 +10、下降 -18、平稳不加分；
    2. 风速 >8m/s 再 -18；
    3. 昼夜温差 >10℃ 再 -6；
    4. 结果夹取到 0~100。

    Args:
        w: camelCase 天气观测（读取 pressureTrend/windSpeed/tempMax/tempMin）。

    Returns:
        tuple[int, str]: (咬口频率得分, 依据文本)。
    """
    s = 75
    reasons = []
    if w.get("pressureTrend") == "up":
        s += 10
        reasons.append("气压回升，咬口转好")
    elif w.get("pressureTrend") == "down":
        s -= 18
        reasons.append("气压下滑，咬口变差")
    else:
        reasons.append("气压平稳")

    ws = w.get("windSpeed")
    if ws is not None and ws > 8:
        s -= 18
        reasons.append("大风影响漂相")

    tmax, tmin = w.get("tempMax"), w.get("tempMin")
    if tmax is not None and tmin is not None and (tmax - tmin) > 10:
        s -= 6
        reasons.append("昼夜温差大")

    if not reasons:
        reasons.append("综合条件平稳")
    return _clamp(s), "；".join(reasons)


def _species_score(w: dict, season: str) -> tuple[int, list[str], str]:
    """鱼种适配度：按气温+季节匹配高适口性鱼种。

    分步说明：
    1. 用内置鱼种的适宜气温区间筛出候选；
    2. 冬季只保留耐寒鱼种，最多取 4 种；
    3. 按候选数量映射分数：>=4 得 90，3 得 78，2 得 60，1 得 40，0 得 20。

    Args:
        w: camelCase 天气观测（读取 temp）。
        season: 季节中文名（春/夏/秋/冬）。

    Returns:
        tuple[int, list[str], str]: (适配度得分, 适宜鱼种列表, 依据文本)。
    """
    t = w.get("temp")
    if t is None:
        t = 15

    cands = [
        ("鲫鱼", 5, 30), ("鲤鱼", 10, 32), ("草鱼", 16, 34),
        ("鳊鱼", 12, 30), ("翘嘴", 12, 28), ("罗非", 20, 35),
    ]
    fish = [name for name, lo, hi in cands if lo <= t <= hi]

    # 季节：冬季优先留耐寒鱼种，夏季留广温鱼种
    if season == "冬季":
        fish = [f for f in fish if f in ("鲫鱼", "鲤鱼", "鳊鱼")] or fish[:2]
    fish = fish[:4]

    if len(fish) >= 4:
        s = 90
    elif len(fish) == 3:
        s = 78
    elif len(fish) == 2:
        s = 60
    elif len(fish) == 1:
        s = 40
    else:
        s = 20

    reason = f"{season}气温{t:.0f}℃，适宜钓{'、'.join(fish) or '少量耐性鱼种'}"
    return _clamp(s), fish, reason


def rule_verdict(weather: dict, date_str: str | None = None) -> dict:
    """确定性规则评分：返回与 LLM 输出同构的可解释结论（0-100 分）。

    计算步骤：
    1. 由日期推断季节；
    2. 分别计算 4 个维度得分与依据；
    3. 按权重加权求和得到总分（夹取 0~100）；
    4. 根据咬口/摄食分生成一句可执行建议。

    Args:
        weather: camelCase 天气观测。
        date_str: 查询日期（ISO），用于季节推断。

    Returns:
        dict: score、dimensions、species、reason、advice。
    """
    season = _season(date_str)
    feed, feed_reason = _feeding_score(weather)
    act, act_reason = _activity_score(weather)
    bite, bite_reason = _bite_score(weather)
    sp, species, sp_reason = _species_score(weather, season)

    dims = [
        {"key": "feeding", "name": "摄食意愿", "score": feed, "reason": feed_reason},
        {"key": "activity", "name": "游动活跃度", "score": act, "reason": act_reason},
        {"key": "bite", "name": "咬口频率", "score": bite, "reason": bite_reason},
        {"key": "species", "name": "鱼种适配度", "score": sp, "reason": sp_reason},
    ]
    for d in dims:
        d["weight"] = _DIM_MAP[d["key"]]["weight"]

    total = _clamp(sum(d["score"] * d["weight"] for d in dims))
    reason = "；".join(d["reason"] for d in dims)

    # 一句可执行建议：优先根据咬口/摄食分给出时段与饵料方向
    if bite >= 80 and feed >= 80:
        advice = f"全天鱼口活跃，建议早口占位打窝，主攻{'、'.join(species[:2])}，拉饵快频率。"
    elif bite >= 65:
        advice = f"鱼口尚可，重点守早口与傍晚两个窗口，用腥香饵诱钓{'、'.join(species[:2])}。"
    else:
        advice = f"今日鱼口偏弱，建议改钓深水或错开中午停口时段，轻灵钓组试{'、'.join(species[:1])}。"

    return {
        "score": total,
        "dimensions": dims,
        "species": species,
        "reason": reason,
        "advice": advice,
    }


# ---------------- 大模型研判 ----------------

# 鱼情研判提示词：固定 JSON 结构与权重口径，要求依据引用具体数值，便于校验。
_LLM_SYSTEM_PROMPT = (
    "你是资深鱼类行为学与垂钓专家。根据给定的结构化天气数据，"
    "结合鱼类摄食行为学规律、不同鱼种生活习性，以及不同季节与地域的垂钓经验，"
    "对该日期该区域鱼情做综合研判。\n"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要多余文字。\n"
    "JSON 结构（字段缺一不可）：\n"
    '{"score":0到100整数,'
    '"dimensions":['
    '{"key":"feeding","name":"摄食意愿","score":0到100,"reason":"依据"},'
    '{"key":"activity","name":"游动活跃度","score":0到100,"reason":"依据"},'
    '{"key":"bite","name":"咬口频率","score":0到100,"reason":"依据"},'
    '{"key":"species","name":"鱼种适配度","score":0到100,"reason":"依据"}],'
    '"species":["鲫鱼","鲤鱼"],'
    '"reason":"总体评分依据（80字内）",'
    '"advice":"一句话出钓建议（60字内，含出钓时段与钓法/饵料方向）"}\n'
    "评分权重（score 需按此加权、与各分项一致）：摄食意愿30%、游动活跃度25%、咬口频率30%、鱼种适配度15%。\n"
    "要求：\n"
    "1. 气温对摄食影响最大，气压趋势与风对咬口影响最大，季节与气温决定鱼种；\n"
    "2. 每条依据必须引用具体数值、可验证，禁止空泛表述；\n"
    "3. score 约等于 dimensions 各分项乘权重的加权和；\n"
    "4. 严禁提及水温、水体温度，所有判断只能基于给定字段中的气温。"
)


def build_brief(weather: dict, place: str | None, date_str: str | None, day_label: str) -> dict:
    """清洗/规整为注入大模型的结构化天气上下文。

    把 camelCase 字段转成中文键值描述，缺失值以 None 占位，
    便于模型直接阅读并减少 hallucination。

    Args:
        weather: camelCase 天气观测。
        place: 地点名。
        date_str: 查询日期。
        day_label: 中文日期标签。

    Returns:
        dict: 中文键名的结构化上下文。
    """
    trend = {"up": "上升", "down": "下降", "flat": "平稳"}.get(weather.get("pressureTrend"), "平稳")
    return {
        "地点": place,
        "日期": f"{date_str or ''}（{day_label}）",
        "季节": _season(date_str),
        "天气现象": weather.get("condition"),
        "气温": f"{weather.get('tempMin')}~{weather.get('tempMax')}℃（当前 {weather.get('temp')}℃，体感 {weather.get('tempFeel')}℃）",
        "气压": f"{weather.get('pressure')}hPa，6小时{weather.get('pressure6h')}（{trend}）",
        "风": f"{weather.get('windDir')}{weather.get('windLevel')}，风速 {weather.get('windSpeed')}m/s",
        "降水量": f"{weather.get('rainfall')}mm",
        "湿度": f"{weather.get('humidity')}%",
        "云量": f"{weather.get('cloudCover')}%",
        "紫外线": weather.get("uv"),
        "能见度": f"{weather.get('visibility')}km",
        "空气质量": f"{weather.get('aqiText')}（AQI {weather.get('aqi')}）",
        "日照时长": _sunshine_hours(weather.get("sunrise"), weather.get("sunset")),
        "日出/日落": f"{weather.get('sunrise')} / {weather.get('sunset')}",
        "黄金窗口": f"上午 {weather.get('goldenAm')}，下午 {weather.get('goldenPm')}",
    }


def _coerce_dimension(item: dict, fallback: dict) -> dict:
    """把 LLM 返回的单个维度规整为 {key,name,score,weight,reason}。

    Args:
        item: LLM 返回的维度对象。
        fallback: 对应 key 的规则引擎维度（作为字段兜底）。

    Returns:
        dict: 规范化后的维度；非 dict 输入直接返回 fallback。
    """
    if not isinstance(item, dict):
        return fallback
    key = item.get("key") or fallback["key"]
    if key not in _DIM_MAP:
        key = fallback["key"]
    name = str(item.get("name") or _DIM_MAP[key]["name"]).strip() or _DIM_MAP[key]["name"]
    raw_score = item.get("score")
    score = _clamp(_num(raw_score, fallback["score"]))
    if raw_score is None:
        score = fallback["score"]
    reason = _scrub_water_terms(str(item.get("reason") or "").strip()) or fallback["reason"]
    return {"key": key, "name": name, "score": score, "weight": _DIM_MAP[key]["weight"], "reason": reason}


def normalize(payload: dict, weather: dict, date_str: str | None, rule: dict) -> dict:
    """把 LLM 返回 JSON 规范化，缺损字段回退规则引擎结果，保证结构完整。

    分步说明：
    1. 维度：按 key 去重并逐个规范化，LLM 漏掉的维度用规则引擎补齐；
    2. 总分：始终用分项得分 × 固定权重回算，不直接采用 LLM 给的总分，
       避免大模型把不同地点统一写成同一个分数（如固定 72）或与分项自相矛盾；
    3. 鱼种 / 依据 / 建议：LLM 为空时分别回退规则引擎值，并做长度截断。

    Args:
        payload: LLM 解析后的 JSON。
        weather: camelCase 天气观测（保持与其他研判函数签名一致，当前未使用）。
        date_str: 查询日期（保持签名一致，当前未使用）。
        rule: rule_verdict 的规则引擎结果（兜底来源）。

    Returns:
        dict: 结构完整的 {score, dimensions, species, reason, advice}。
    """
    if not isinstance(payload, dict):
        return rule

    fallback_dim = {d["key"]: d for d in rule["dimensions"]}
    raw_dims = payload.get("dimensions")
    if isinstance(raw_dims, list):
        used = set()
        dims = []
        for item in raw_dims:
            fb = fallback_dim.get(item.get("key") if isinstance(item, dict) else None)
            if fb is None or fb["key"] in used:
                continue
            dims.append(_coerce_dimension(item, fb))
            used.add(fb["key"])
        # 补齐 LLM 漏掉的维度
        for key in fallback_dim:
            if key not in used:
                dims.append(dict(fallback_dim[key]))
    else:
        dims = [dict(d) for d in rule["dimensions"]]

    # 总分始终按固定权重回算：LLM 只负责分项判断，不能直接指定总分。
    # 这样不同地点的总分严格由分项差异驱动，不会出现固定 72 分。
    score = _clamp(sum(d["score"] * d["weight"] for d in dims))

    species_payload = payload.get("species")
    species = (
        [str(x).strip() for x in species_payload if str(x).strip()][:6]
        if isinstance(species_payload, list)
        else []
    ) or rule["species"]

    reason = _scrub_water_terms(str(payload.get("reason") or "").strip())[:120] or rule["reason"]
    advice = _scrub_water_terms(str(payload.get("advice") or "").strip())[:80] or rule["advice"]

    return {
        "score": score,
        "dimensions": dims,
        "species": species,
        "reason": reason,
        "advice": advice,
    }


async def judge(weather: dict, place: str | None, date_str: str | None, day_label: str) -> dict:
    """鱼情研判主入口：大模型可用走 LLM，否则走规则引擎；返回带 aiGenerated 标记的结论。

    Args:
        weather: camelCase 天气观测。
        place: 地点名。
        date_str: 查询日期。
        day_label: 中文日期标签。

    Returns:
        dict: ``{"result": 结论, "aiGenerated": bool}``；LLM 失败时 result 为规则引擎结果。
    """
    rule = rule_verdict(weather, date_str)
    if not llm.enabled():
        return {"result": rule, "aiGenerated": False}

    try:
        messages = [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                build_brief(weather, place, date_str, day_label), ensure_ascii=False
            )},
        ]
        raw = await llm.chat(messages, max_tokens=1024, temperature=0.3, thinking=False)
        result = normalize(llm.loads_json(raw), weather, date_str, rule)
        return {"result": result, "aiGenerated": True}
    except llm.LLMError:
        return {"result": rule, "aiGenerated": False}