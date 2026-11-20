"""tests/test_weather.py — 天气数据生成（大模型提示词 + 兜底合并）测试

验证：
1. 提示词覆盖网页端天气报告的完整字段（气温范围 / 当前温度 / AQI / 湿度 / 日出日落 / 风力含「转」等）；
2. 大模型返回的 JSON 能正确合并到兜底结构，缺失/非法字段回退；
3. 大模型未配置时 generate_weather 回退确定性数据且结构完整（接口不报错）。

执行方式：在 fishing-ai-api 目录运行 `pytest tests/test_weather.py`；
异步用例依赖 pytest.ini 的 asyncio_mode = auto。

前置条件与注意事项：
- 除 test_generate_weather_falls_back_without_llm 外均为纯函数测试，不依赖网络；
- 该用例通过 monkeypatch 同时关闭 llm 与 qweather，确保走确定性兜底路径。
"""

from app.services import ai, qweather


def test_weather_prompt_covers_required_fields():
    """提示词必须覆盖与网页端一致的完整天气字段。

    前置条件：读取 ai._WEATHER_SYSTEM_PROMPT 常量。
    预期行为：天况、温度、体感、湿度、AQI、风向风力、日出日落、气压、评分
    等字段名均出现在提示词中。
    断言意图：防止调整提示词时遗漏字段，导致大模型输出残缺结构。
    """
    prompt = ai._WEATHER_SYSTEM_PROMPT
    # 网页端天气报告的关键字段
    required = [
        "condition", "temp", "temp_max", "temp_min", "temp_feel",
        "humidity", "aqi", "aqi_text", "wind_dir", "wind_level",
        "sunrise", "sunset", "pressure", "score",
    ]
    for field in required:
        assert field in prompt, f"提示词缺少字段 {field}"


def test_qweather_icon_mapping():
    """QWeather 图标代码映射到本项目图标名。

    前置条件：无（直接调用 qweather._map_icon）。
    预期行为：100/150 -> sun，103 -> partly，305/400 -> rain，104/500 -> cloud。
    断言意图：保证第三方代码与前端图标名稳定对应。
    """
    assert qweather._map_icon("100") == "sun"
    assert qweather._map_icon("150") == "sun"
    assert qweather._map_icon("103") == "partly"
    assert qweather._map_icon("305") == "rain"
    assert qweather._map_icon("400") == "rain"
    assert qweather._map_icon("104") == "cloud"
    assert qweather._map_icon("500") == "cloud"


def test_qweather_wind_scale():
    """风力等级：同等级不重复，昼夜不同用「转」连接。

    前置条件：无（直接调用 qweather._wind_scale_str）。
    预期行为：日 / 夜等级不同时输出「A级转B级」；相同时只输出一次；
    任一侧为空时取非空值；两侧皆空返回空字符串。
    断言意图：避免出现「3级转3级」这类冗余文案。
    """
    assert qweather._wind_scale_str("4-5", "3-4") == "4-5级转3-4级"
    assert qweather._wind_scale_str("4", "4") == "4级"
    assert qweather._wind_scale_str("3", "") == "3级"
    assert qweather._wind_scale_str("", "3") == "3级"
    assert qweather._wind_scale_str("", "") == ""


def test_merge_weather_obs_applies_valid_fields():
    """大模型返回的完整观测能覆盖兜底值，且数值/文本分别被正确归一。

    前置条件：base 为 _base_weather 的兜底结构；payload 为一份完整观测。
    预期行为：place 保留兜底值（不由观测覆盖），其余字段（天况、温度、
    湿度、AQI、风向风力、日出日落、评分）均采用 payload 值。
    断言意图：确认合并策略为「字段级覆盖」而非整结构替换。
    """
    base = ai._base_weather("安徽省 合肥市 蜀山区", "蜀山区·3公里范围内", "2026-11-19")

    payload = {
        "condition": "阴转晴",
        "temp": 8,
        "temp_max": 12,
        "temp_min": 0,
        "temp_feel": 6,
        "pressure": 1026.4,
        "pressure_6h": "+2.1",
        "pressure_trend": "up",
        "wind_dir": "西北风",
        "wind_level": "4-5级转3-4级",
        "wind_speed": 6.2,
        "humidity": 57,
        "cloud_cover": 60,
        "rainfall": 0,
        "uv": 3,
        "visibility": 15,
        "aqi": 47,
        "aqi_text": "优",
        "moon_phase": "下弦月",
        "moon_illum": 34,
        "sunrise": "06:42",
        "sunset": "17:09",
        "golden_am": "06:42-08:10",
        "golden_pm": "15:40-17:09",
        "solunar": "小潮·鱼口偏弱",
        "score": 87,
    }

    w = ai._merge_weather_obs(payload, base)

    assert w["place"] == "安徽省 合肥市 蜀山区"  # place 由兜底保留
    assert w["condition"] == "阴转晴"
    assert w["temp"] == 8
    assert w["temp_max"] == 12
    assert w["temp_min"] == 0
    assert w["humidity"] == 57
    assert w["aqi"] == 47
    assert w["aqi_text"] == "优"
    assert w["wind_dir"] == "西北风"
    assert w["wind_level"] == "4-5级转3-4级"
    assert w["sunrise"] == "06:42"
    assert w["sunset"] == "17:09"
    assert w["score"] == 87


def test_merge_weather_obs_rejects_invalid_values():
    """非法数值回退兜底、非法字符串忽略，保证结构完整。

    前置条件：payload 中 temp="abc"（非数字）、condition=""（空串）、humidity=99（合法）。
    预期行为：temp 与 condition 保持兜底值，humidity 正常覆盖为 99。
    断言意图：确认单字段脏数据不会污染整体结构或抛异常。
    """
    base = ai._base_weather("杭州", "西湖区", None)
    w = ai._merge_weather_obs({"temp": "abc", "condition": "", "humidity": 99}, base)

    assert w["temp"] == base["temp"]          # 非法数值 -> 兜底
    assert w["condition"] == base["condition"]  # 空字符串 -> 兜底
    assert w["humidity"] == 99                # 合法数值正常覆盖


async def test_generate_weather_falls_back_without_llm(monkeypatch):
    """大模型/天气源未配置时返回结构完整的确定性数据（离线、确定）。

    前置条件：monkeypatch 关闭 llm.enabled 与 qweather.enabled。
    预期行为：返回 weather / verdict / slots 三段；weather.place 为传入地点，
    且温度、湿度、日出、日落等关键字段存在。
    断言意图：保证无任何外部依赖时接口仍可返回可用数据。
    """
    from app.services import llm, qweather

    monkeypatch.setattr(llm, "enabled", lambda: False)
    monkeypatch.setattr(qweather, "enabled", lambda: False)

    data = await ai.generate_weather("安徽省 合肥市 蜀山区", "蜀山区·3公里范围内", "2026-11-19")
    assert "weather" in data
    assert "verdict" in data
    assert "slots" in data
    w = data["weather"]
    assert w["place"] == "安徽省 合肥市 蜀山区"
    assert "temp" in w and "humidity" in w and "sunrise" in w and "sunset" in w


def test_finalize_shape_matches_frontend_contract():
    """最终输出 camelCase 字段与前端（weather.vue）消费结构一致。

    前置条件：build_weather("杭州", "西湖区", None) 走确定性兜底。
    预期行为：weather 含 tempMax / tempMin / tempFeel / windDir / windLevel /
    sunrise / sunset / aqiText 等 camelCase 键，verdict 含 advice。
    断言意图：防止后端字段蛇形命名或缺失导致前端渲染空值。
    """
    data = ai.build_weather("杭州", "西湖区", None)
    w = data["weather"]
    for key in ("tempMax", "tempMin", "tempFeel", "windDir", "windLevel", "sunrise", "sunset", "aqiText"):
        assert key in w, f"缺少 camelCase 字段 {key}"
    assert "advice" in data["verdict"]


def test_build_slots_recomputes_levels_from_probability():
    """每个时段的口型必须由其概率按统一阈值生成，而不是沿用模板标签。

    前置条件：整体鱼情分为 90 / 72 / 58 / 22 四档。
    预期行为：8 个时段的 level/name 均等于 verdict_of(该时段 p) 的结果，
    p 始终落在 5~99。
    断言意图：证明全天时段口型严格遵循爆户/连杆/有口/慢口/停口的分级标准。
    """
    for overall in (90, 72, 58, 22):
        slots = ai._build_slots(overall)
        assert len(slots) == 8
        for slot in slots:
            expected = ai.verdict_of(slot["p"])
            assert slot["level"] == expected["level"], (overall, slot)
            assert slot["name"] == expected["name"], (overall, slot)
            assert 5 <= slot["p"] <= 99


def test_rainfall_preserved_and_legacy_fields_removed():
    """降水量保留并 camelCase 输出；水温与降水概率字段不再出现。

    前置条件：上游只给 rainfall=12.3。
    预期行为：最终 weather.rainfall 为 12.3，且不再包含 waterTemp / rainProb。
    断言意图：保证降水量完整透传到前端，同时确认两个下线字段已彻底移除。
    """
    base = ai._base_weather("浙江省 杭州市 西湖区", None, "2026-11-20")
    merged = ai._merge_weather_obs({"rainfall": 12.3}, base)
    data = ai._finalize(merged, "2026-11-20")
    w = data["weather"]

    assert w["rainfall"] == 12.3
    assert "waterTemp" not in w
    assert "rainProb" not in w