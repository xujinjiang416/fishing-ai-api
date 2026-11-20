"""config.py — 服务配置（读取 .env）与业务常量

职责：
    1. 通过 pydantic-settings 读取环境变量 / 项目根目录 .env，生成全局单例 settings；
    2. 集中承载数据库、鉴权、积分、广告、大模型、天气源、微信、后台账号等可调参数。

关键流程：
    模块导入时实例化 Settings（字段名不区分大小写，如 DATABASE_URL / database_url 均可），
    业务代码统一 `from app.core.config import settings` 读取，不再散落硬编码。

注意事项：
    - 积分/广告等字段只是「默认值」，运行时可被 app_configs 表（后台积分配置）覆盖；
    - 涉及密钥的字段默认值仅用于本地开发，生产环境必须通过 .env 覆盖。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置模型（字段即配置项，默认值即兜底值）。

    所有字段均带默认值，保证「零配置可启动」；实际部署时用 .env 覆盖。
    """

    # env_file 指定相对当前工作目录的 .env；extra="ignore" 忽略未声明变量
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "渔获 AI · 后端服务"          # 应用名称，展示在 /docs 标题
    api_prefix: str = "/api"                       # 所有业务路由的统一前缀
    debug: bool = True                             # 调试开关（生产环境应置为 False）

    # ---- 数据库 ----
    # 连接串格式：方言+驱动://用户:密码@主机:端口/库名?charset=utf8mb4；
    # 异步引擎需 aiomysql 驱动，实际部署请在 .env 中覆盖为 mysql+aiomysql://...
    database_url: str = "mysql+pymysql://root:root@127.0.0.1:3306/fishing_ai?charset=utf8mb4"

    # ---- 鉴权 ----
    jwt_secret: str = "dev-only-secret-change-me"  # JWT 签名密钥，生产必须替换
    jwt_algorithm: str = "HS256"                   # JWT 签名算法（对称密钥）
    jwt_expire_minutes: int = 60 * 24 * 30         # token 有效期：60*24*30 分钟 = 30 天

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
    llm_enabled: bool = True                                 # 是否启用大模型路径
    llm_base_url: str = "https://api.deepseek.com"           # 服务地址（不带具体路径）
    llm_path: str = "/chat/completions"                      # 对话补全接口路径
    llm_api_key: str = ""                                    # 密钥；为空则走本地兜底算法
    llm_auth_header: str = "Authorization"   # 鉴权头名，如 x-ide-token
    llm_auth_scheme: str = "Bearer"          # 鉴权前缀，如 Bearer（x-ide-token 不需要）
    # DeepSeek-V4.1-Flash 的官方模型 id；思维链模型，max_tokens 需留足思考余量
    llm_model: str = "deepseek-flash"                        # 调用的模型 id
    llm_stream: bool = False                                 # 是否流式返回（后端当前按非流式消费）
    llm_timeout: float = 30.0                                # 单次请求超时（秒）
    llm_max_tokens: int = 1024                               # 单次生成的最大 token 数

    # ---- 真实天气数据源（和风天气 QWeather；未配置 key/Host 时天气走大模型/兜底路径）----
    # 注：和风天气已改用「专属 API Host」，仅为 key 而缺少 host 时返回 403 Invalid Host。
    # Host 形如 `abc123.xyz.qweatherapi.com`，在控制台「设置 -> API Host」查看。
    qweather_key: str = ""                                   # 和风天气 API Key
    qweather_host: str = ""                                  # 和风天气专属 API Host

    # ---- 微信（留空则走模拟实现） ----
    wechat_appid: str = ""                                   # 小程序 AppID
    wechat_secret: str = ""                                  # 小程序 AppSecret
    # 内置演示账号（手机号 + 密码可直接登录，便于免注册体验完整数据）
    demo_phone: str = "13800138000"                          # 演示账号手机号
    demo_password: str = "123456"                            # 演示账号密码

    # ---- 管理后台（admins 表与小程序 users 表分开；可用 .env 覆盖）----
    admin_username: str = "admin"                            # 初始化超级管理员账号
    admin_password: str = "meng01040416"                     # 初始化超级管理员密码


# 全局配置单例：模块导入时即读取 .env，其余模块直接引用该对象
settings = Settings()
