"""tests/test_weather.py — 天气数据生成（大模型提示词 + 兜底合并）测试

验证：
1. 提示词覆盖网页端天气报告的完整字段（气温范围 / 当前温度 / AQI / 湿度 / 日出日落 / 风力含「转」等）；
2. 大模型返回的 JSON 能正确合并到兜底结构，缺失/非法字段回退；
3. 大模型未配置时 generate_weather 回退确定性数据且结构完整（接口不报错）。
"""

from app.services import ai, qweather


def test_weather_prompt_covers_required_fields():
    """提示词必须覆盖与网页端一致的完整天气字段。"""
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
    """QWeather 图标代码映射到本项目图标名。"""
    assert qweather._map_icon("100") == "sun"
    assert qweather._map_icon("150") == "sun"
    assert qweather._map_icon("103") == "partly"
    assert qweather._map_icon("305") == "rain"
    assert qweather._map_icon("400") == "rain"
    assert qweather._map_icon("104") == "cloud"
    assert qweather._map_icon("500") == "cloud"


def test_qweather_wind_scale():
    """风力等级：同等级不重复，昼夜不同用「转」连接。"""
    assert qweather._wind_scale_str("4-5", "3-4") == "4-5级转3-4级"
    assert qweather._wind_scale_str("4", "4") == "4级"
    assert qweather._wind_scale_str("3", "") == "3级"
    assert qweather._wind_scale_str("", "3") == "3级"
    assert qweather._wind_scale_str("", "") == ""


def test_merge_weather_obs_applies_valid_fields():
    """大模型返回的完整观测能覆盖兜底值，且数值/文本分别被正确归一。"""
    base = ai._base_weather("安徽省 合肥市 蜀山区", "蜀山区·3公里范围内", "2026-11-19")

    payload = {
        "condition": "阴转晴",
        "temp": 8,
        "temp_max": 12,
        "temp_min": 0,
        "temp_feel": 6,
        "water_temp": 9.5,
        "pressure": 1026.4,
        "pressure_6h": "+2.1",
        "pressure_trend": "up",
        "wind_dir": "西北风",
        "wind_level": "4-5级转3-4级",
        "wind_speed": 6.2,
        "humidity": 57,
        "cloud_cover": 60,
        "rain_prob": 5,
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
    """非法数值回退兜底、非法字符串忽略，保证结构完整。"""
    base = ai._base_weather("杭州", "西湖区", None)
    w = ai._merge_weather_obs({"temp": "abc", "condition": "", "humidity": 99}, base)

    assert w["temp"] == base["temp"]          # 非法数值 -> 兜底
    assert w["condition"] == base["condition"]  # 空字符串 -> 兜底
    assert w["humidity"] == 99                # 合法数值正常覆盖


async def test_generate_weather_falls_back_without_llm(monkeypatch):
    """大模型/天气源未配置时返回结构完整的确定性数据（离线、确定）。"""
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
    """最终输出 camelCase 字段与前端（weather.vue）消费结构一致。"""
    data = ai.build_weather("杭州", "西湖区", None)
    w = data["weather"]
    for key in ("tempMax", "tempMin", "tempFeel", "windDir", "windLevel", "sunrise", "sunset", "aqiText"):
        assert key in w, f"缺少 camelCase 字段 {key}"
    assert "advice" in data["verdict"]