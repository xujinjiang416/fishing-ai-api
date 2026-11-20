"""tests/test_verdict.py — 鱼情评分系统测试

覆盖需求 2/3/4/5：
- 评分维度与权重体系（权重和为 1.0）；
- 确定性规则引擎在「好天气 / 坏天气」多场景下的分显差异与总分一致性；
- 大模型 JSON 归一化与字段兜底；
- judge() 无大模型时回退规则引擎且结构完整。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_verdict.py`；
最后一个异步用例依赖 pytest.ini 的 asyncio_mode = auto。

前置条件与注意事项：
- 除 test_judge_weather_without_llm_uses_rule_engine 外均为纯函数测试，不依赖数据库与网络；
- 该异步用例用 monkeypatch 关闭 llm.enabled，保证离线且结果确定。
"""

from app.services import ai, verdict


def _weather(**over):
    """构造 camelCase 天气观测（对齐 ai._finalize 对外结构）。

    Args:
        **over: 需要覆盖的基础字段，如 waterTemp / pressureTrend / windSpeed。

    Returns:
        dict: 包含地点、天况、温度、气压、风、湿度、日出日落与潮汐等完整键的字典。
    """
    base = {
        "place": "安徽省 合肥市 蜀山区",
        "condition": "多云",
        "conditionIcon": "partly",
        "temp": 18, "tempMax": 22, "tempMin": 12, "tempFeel": 17, "waterTemp": 16.5,
        "pressure": 1018.6, "pressure6h": "+2.0", "pressureTrend": "up",
        "windDir": "东北风", "windLevel": "2级", "windSpeed": 3.0,
        "humidity": 60, "cloudCover": 40, "rainProb": 10, "rainfall": 0,
        "uv": 4, "visibility": 15, "aqi": 47, "aqiText": "优",
        "sunrise": "06:42", "sunset": "17:09", "goldenAm": "06:42-08:10",
        "goldenPm": "15:40-17:09", "solunar": "小潮·鱼口偏弱",
    }
    base.update(over)
    return base


def test_dimensions_weights_sum_to_one():
    """评分维度权重必须总和为 1.0。

    前置条件：无（直接读取 verdict.SCORE_DIMENSIONS 常量）。
    预期行为：4 个维度的 weight 之和为 1（容差 1e-9）。
    断言意图：防止调整权重后总分被放大或缩小。
    """
    total = sum(d["weight"] for d in verdict.SCORE_DIMENSIONS)
    assert abs(total - 1.0) < 1e-9
    assert len(verdict.SCORE_DIMENSIONS) == 4


def test_rule_verdict_structure_complete():
    """规则引擎输出 0-100 分 + 4 维度 + 鱼种 + 依据 + 建议。

    前置条件：使用 _weather() 的标准观测（多云、18℃、气压 1018.6）。
    预期行为：score 在 0-100；dimensions 恰为 4 项且每项分数在 0-100 并有 reason；
    species 为非空列表；reason 与 advice 均非空。
    断言意图：保证规则结果可直接渲染为报告，不存在缺字段。
    """
    r = verdict.rule_verdict(_weather(), "2026-11-19")
    assert 0 <= r["score"] <= 100
    assert len(r["dimensions"]) == 4
    for d in r["dimensions"]:
        assert 0 <= d["score"] <= 100
        assert d["reason"]
    assert isinstance(r["species"], list) and r["species"]
    assert r["reason"]
    assert r["advice"]


def test_rule_verdict_total_matches_weighted_sum():
    """总分等于各维度得分乘权重的加权和。

    前置条件：同上标准观测。
    预期行为：rule_verdict 返回的 score 等于 round(Σ 分数 × 权重) 并夹取到 0-100。
    断言意图：确认总分不是独立拍脑袋计算，而与维度得分严格一致。
    """
    r = verdict.rule_verdict(_weather(), "2026-11-19")
    expected = round(sum(d["score"] * d["weight"] for d in r["dimensions"]))
    assert r["score"] == max(0, min(100, expected))


def test_good_weather_scores_higher_than_bad():
    """多场景验证：好天气总分应显著高于坏天气。

    前置条件：
    - 好天气：水温 18℃、气温 20℃、气压上升、微风、低降雨概率；
    - 坏天气：水温 2℃、气温 1℃、气压下降、9 级风、85% 降雨、AQI 180。
    预期行为：好天气得分高于坏天气，且好天气应落入 80 分以上的高分档。
    断言意图：用极端对照验证规则引擎的单调性与区分度。
    """
    good = verdict.rule_verdict(
        _weather(waterTemp=18, temp=20, pressureTrend="up", windSpeed=2, rainProb=5, humidity=60),
        "2026-10-20",
    )
    bad = verdict.rule_verdict(
        _weather(waterTemp=2, temp=1, tempMax=3, tempMin=-4,
                 pressureTrend="down", windSpeed=9, rainProb=85, humidity=98, aqi=180, aqiText="中度"),
        "2026-01-10",
    )
    assert good["score"] > bad["score"]
    assert good["score"] >= 80  # 好天气应落到高分档
    assert bad["score"] < good["score"]


def test_normalize_merges_and_falls_back():
    """LLM 返回部分字段时：合法值保留、缺失维度回退规则结果。

    前置条件：payload 仅给出 score=88、一个 feeding 维度（92 分）与 species。
    预期行为：总分采用 88；维度补齐为 4 项且 feeding 保留 92；
    species 使用 LLM 结果；缺失的 reason 回退规则引擎文案。
    断言意图：确认归一化是「合并 + 补齐」而非全量覆盖或全量丢弃。
    """
    rule = verdict.rule_verdict(_weather(), "2026-11-19")
    payload = {
        "score": 88,
        "dimensions": [
            {"key": "feeding", "name": "摄食意愿", "score": 92, "reason": "水温16℃适宜"},
        ],
        "species": ["鲫鱼", "鲤鱼"],
    }
    out = verdict.normalize(payload, _weather(), "2026-11-19", rule)

    assert out["score"] == 88
    assert len(out["dimensions"]) == 4  # 缺失维度补齐
    feeding = next(d for d in out["dimensions"] if d["key"] == "feeding")
    assert feeding["score"] == 92
    assert out["species"] == ["鲫鱼", "鲤鱼"]
    assert out["reason"] == rule["reason"]   # 未提供 reason 回退规则


def test_normalize_invalid_score_clamps():
    """非法/越界分数被夹取到 0-100。

    前置条件：分别传入 score=200 与非数字字符串 "abc"。
    预期行为：200 被夹取为 100；"abc" 回退或夹取后仍落在 0-100 区间。
    断言意图：保证接口输出的分数永远合法，前端无需容错。
    """
    rule = verdict.rule_verdict(_weather(), "2026-11-19")
    out = verdict.normalize({"score": 200}, _weather(), "2026-11-19", rule)
    assert out["score"] == 100
    out2 = verdict.normalize({"score": "abc"}, _weather(), "2026-11-19", rule)
    assert 0 <= out2["score"] <= 100


def test_sunshine_hours():
    """日照时长计算：跨天兜底、非法输入返回「未知」、正常输入格式合法。

    前置条件：无（直接调用 verdict._sunshine_hours）。
    预期行为：
    - 日落早于日出（18:00 -> 06:00）按跨天计算，得 12.0 小时；
    - 无法解析的输入返回「未知」；
    - 正常输入匹配「N 或 N.N 小时」格式。
    断言意图：确认展示文案既正确又不出现非法小数位。
    """
    assert verdict._sunshine_hours("18:00", "06:00") == "12.0 小时"  # 跨天兜底
    assert verdict._sunshine_hours("bad", "17:00") == "未知"
    # 日照时长格式为「N.N 小时」或「N 小时」
    import re

    assert re.fullmatch(r"\d+(\.\d)? 小时", verdict._sunshine_hours("06:42", "17:09"))


async def test_judge_weather_without_llm_uses_rule_engine(monkeypatch):
    """无大模型时 judge_weather 走规则引擎，且 verdict 结构完整（可解释）。

    前置条件：monkeypatch 强制 llm.enabled 为 False，build_weather 生成标准观测。
    预期行为：verdict 分数 0-100，level / name / rank / advice / reason 均存在，
    dimensions 为 4 项，species 非空，aiGenerated 明确为 False；
    slots 的开口概率均落在 5-99。
    断言意图：保证未配置大模型时接口仍可用且标注来源为规则引擎。
    """
    from app.services import llm

    # 强制关闭 LLM，保证测试离线、确定、不依赖网络/API Key
    monkeypatch.setattr(llm, "enabled", lambda: False)

    data = ai.build_weather("安徽省 合肥市 蜀山区", "蜀山区·3公里范围内", "2026-11-19")
    out = await ai.judge_weather(data, "今天")

    v = out["verdict"]
    assert 0 <= v["score"] <= 100
    assert v["level"] and v["name"] and v["rank"]
    assert v["advice"] and v["reason"]
    assert len(v["dimensions"]) == 4
    assert isinstance(v["species"], list) and v["species"]
    assert v["aiGenerated"] is False
    # 时段概率已按最终得分重算
    assert all(5 <= s["p"] <= 99 for s in out["slots"])