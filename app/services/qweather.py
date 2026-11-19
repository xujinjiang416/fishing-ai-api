"""qweather.py — 和风天气（QWeather）真实天气数据源适配

把 QWeather 的实时/3 天预报/空气质量三类接口，映射成本项目内部 `snake_case`
天气观测结构（与 seed_data.WEATHER 同构，供 ai._merge_weather_obs 合并）。

设计约定：
- 未配置 `QWEATHER_KEY` + `QWEATHER_HOST` 时 enabled() 为 False，上层自动回退大模型/确定性数据；
- 任何一步（地名解析 / 请求 / 解析 / 业务 code 非 200）失败都抛 QWeatherError，
  由调用方决定降级，接口不因天气源异常而报错；
- 只返回「能可靠填」的字段；月相/潮汐/黄金窗口/气压趋势等 QWeather 免费版
  不直接提供，交由大模型或兜底数据补齐。

参考文档：https://dev.qweather.com/docs/api/weather/weather-now/
"""

import httpx

from ..core.config import settings

# 和风天气自 2026 年起废弃公共域名（api/devapi/geoapi.qweather.com），
# 一律改为「账号专属 API Host（QWEATHER_HOST） + 端点路径」拼接请求地址。
def _api_base() -> str:
    """返回专属 API Host 的基础地址（自动补 https）。"""
    host = (settings.qweather_host or "").strip().rstrip("/")
    if not host:
        return ""
    return host if host.startswith("https://") else f"https://{host}"


GEO_PATH = "/geo/v2/city/lookup"
WEATHER_NOW_PATH = "/v7/weather/now"
WEATHER_3D_PATH = "/v7/weather/3d"
AIR_QUALITY_PATH = "/airquality/v1/current"   # 实时空气质量（按经纬度，cn-mee 即中国 AQI）
WEATHER_CURRENT_PATH = "/weather/v1/current"   # 实时天气（含紫外线指数 uvIndex）


class QWeatherError(Exception):
    """天气源调用失败（网络 / 鉴权 / 无匹配城市 / 业务 code 非 200）。"""


def enabled() -> bool:
    return bool(settings.qweather_key and settings.qweather_host)


async def _get_json(url: str, params: dict) -> dict:
    """发起一次 QWeather 请求并解析；非 200 或业务 code 非 200 抛 QWeatherError。"""
    headers = {"X-QW-Api-Key": settings.qweather_key}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise QWeatherError(f"天气源请求失败：{exc}") from exc

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json())
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:200]
        raise QWeatherError(f"天气源返回 HTTP {resp.status_code}：{detail}")

    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise QWeatherError("天气源返回不可解析") from exc

    # 旧接口用业务 code 表示成败；新 v1 接口无 code 字段（HTTP 200 即成功），仅在有 code 时校验。
    code = body.get("code")
    if code is not None and str(code) != "200":
        raise QWeatherError(f"天气源业务错误：{code}")

    return body


async def _lookup_location(place: str) -> dict | None:
    """用 GeoAPI 把中文地名（省 市 区）解析成 QWeather Location（含 id/lat/lon）。"""
    body = await _get_json(_api_base() + GEO_PATH, {"location": place})
    locations = body.get("location") or []
    return locations[0] if locations else None


def _map_icon(code: str) -> str:
    """QWeather 天气图标代码 -> 本项目图标名（sun/cloud/partly/rain）。"""
    c = str(code or "")
    if c in ("100", "150"):
        return "sun"
    if c in ("102", "103"):
        return "partly"
    if c.startswith("3") or c.startswith("4"):
        return "rain"
    return "cloud"


def _wind_scale_str(day_scale: str, night_scale: str) -> str:
    """把风力等级组装成「4-5级转3-4级」样式；白天与夜间相同时只给一段。"""
    day = f"{day_scale}级" if day_scale else ""
    night = f"{night_scale}级" if night_scale else ""
    if day and night and night != day:
        return f"{day}转{night}"
    return day or night or ""


async def fetch_weather(place: str) -> dict:
    """查询该地点的真实天气并映射为内部 snake_case 观测结构。

    返回仅包含能可靠映射的字段（缺的字段由调用方回退补齐）；解析失败抛 QWeatherError。
    """
    loc = await _lookup_location(place)
    if not loc:
        raise QWeatherError(f"未在地名下解析到城市：{place}")
    location_id = loc.get("id") or ""
    lat = loc.get("lat") or ""
    lon = loc.get("lon") or ""

    now_body = await _get_json(_api_base() + WEATHER_NOW_PATH, {"location": location_id})
    daily_body = await _get_json(_api_base() + WEATHER_3D_PATH, {"location": location_id})
    now = now_body.get("now") or {}
    daily = (daily_body.get("daily") or [{}])[0]

    # AQI：新实时空气质量接口（按经纬度），cn-mee 为中国标准
    aqi = aqi_text = None
    try:
        air_body = await _get_json(_api_base() + f"{AIR_QUALITY_PATH}/{lat}/{lon}", {"lang": "zh"})
        indexes = air_body.get("indexes") or []
        for idx in indexes:
            if idx.get("code") == "cn-mee":
                aqi = idx.get("aqi")
                aqi_text = idx.get("category")
                break
        if aqi is None and indexes:
            aqi = indexes[0].get("aqi")
            aqi_text = indexes[0].get("category")
    except QWeatherError:
        pass  # 空气质量失败不影响其它字段

    # 紫外线：新实时天气接口（按经纬度）的 uvIndex
    uv = None
    try:
        wx_body = await _get_json(_api_base() + f"{WEATHER_CURRENT_PATH}/{lat}/{lon}", {})
        uv = wx_body.get("uvIndex")
    except QWeatherError:
        pass  # 紫外线失败不影响其它字段

    # 天气现象：今天「白天转夜间」，无夜间时用实时文本
    text_day = daily.get("textDay") or now.get("text") or ""
    text_night = daily.get("textNight") or ""
    condition = f"{text_day}转{text_night}" if (text_day and text_night and text_night != text_day) else (text_day or "晴")

    # 风力：风向单独取，级数「白天转夜间」（对齐「西北风 4-5级转3-4级」这种表达）
    wind_scale_day = daily.get("windScaleDay") or now.get("windScale") or ""
    wind_scale_night = daily.get("windScaleNight") or ""
    wind_level = _wind_scale_str(wind_scale_day, wind_scale_night)

    wind_dir = daily.get("windDirDay") or now.get("windDir") or ""

    wind_speed_kmh = now.get("windSpeed")
    wind_speed_ms = round(float(wind_speed_kmh) / 3.6, 1) if wind_speed_kmh is not None else None

    return {
        "condition": condition,
        "condition_icon": _map_icon(now.get("icon") or ""),
        "temp": now.get("temp"),
        "temp_max": daily.get("tempMax"),
        "temp_min": daily.get("tempMin"),
        "temp_feel": now.get("feelsLike"),
        "humidity": now.get("humidity"),
        "pressure": now.get("pressure"),
        "wind_dir": wind_dir,
        "wind_level": wind_level,
        "wind_speed": wind_speed_ms,
        "cloud_cover": now.get("cloud"),
        "rain_prob": daily.get("precip"),
        "rainfall": now.get("precip"),
        "uv": uv,
        "visibility": now.get("vis"),
        "aqi": aqi,
        "aqi_text": aqi_text,
        "sunrise": daily.get("sunrise"),
        "sunset": daily.get("sunset"),
    }