"""config.py — 服务配置（读取 .env）与业务常量

所有可调参数集中在此，页面/业务代码禁止再散落硬编码。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "渔获 AI · 后端服务"
    api_prefix: str = "/api"
    debug: bool = True

    # ---- 数据库 ----
    database_url: str = "mysql+pymysql://root:root@127.0.0.1:3306/fishing_ai?charset=utf8mb4"

    # ---- 鉴权 ----
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 30

    # ---- 积分与广告（此处为「默认值」，运行时可被 app_configs 表 / 后台积分配置覆盖）----
    points_per_yuan: int = 150          # 150 积分 = 1 元
    ad_points: int = 5                  # 看一条广告得 5 积分
    ad_seconds: int = 30                # 单条广告时长
    ad_limit_normal: int = 15           # 普通用户每日广告额度
    ad_limit_member: int = 20           # 会员每日广告额度

    # ---- 签到（签到积分按会员/非会员分别配置，默认 会员 5 分、非会员 1 分）----
    checkin_member_points: int = 5      # 会员每日签到积分
    checkin_normal_points: int = 1      # 非会员每日签到积分

    # ---- 大模型（OpenAI 兼容协议；未配置 api_key 时自动退回本地确定性算法） ----
    llm_enabled: bool = True
    llm_base_url: str = "https://api.deepseek.com"
    llm_path: str = "/chat/completions"
    llm_api_key: str = ""
    llm_auth_header: str = "Authorization"   # 鉴权头名，如 x-ide-token
    llm_auth_scheme: str = "Bearer"          # 鉴权前缀，如 Bearer（x-ide-token 不需要）
    # DeepSeek-V4.1-Flash 的官方模型 id；思维链模型，max_tokens 需留足思考余量
    llm_model: str = "deepseek-flash"
    llm_stream: bool = False
    llm_timeout: float = 30.0
    llm_max_tokens: int = 1024

    # ---- 真实天气数据源（和风天气 QWeather；未配置 key/Host 时天气走大模型/兜底路径）----
    # 注：和风天气已改用「专属 API Host」，仅为 key 而缺少 host 时返回 403 Invalid Host。
    # Host 形如 `abc123.xyz.qweatherapi.com`，在控制台「设置 -> API Host」查看。
    qweather_key: str = ""
    qweather_host: str = ""

    # ---- 微信（留空则走模拟实现） ----
    wechat_appid: str = ""
    wechat_secret: str = ""
    # 内置演示账号（手机号 + 密码可直接登录，便于免注册体验完整数据）
    demo_phone: str = "13800138000"
    demo_password: str = "123456"

    # ---- 管理后台（admins 表与小程序 users 表分开；可用 .env 覆盖）----
    admin_username: str = "admin"
    admin_password: str = "meng01040416"


settings = Settings()
