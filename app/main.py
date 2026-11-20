"""main.py — FastAPI 应用入口

职责：
    1. 创建 FastAPI 应用实例，挂载 CORS 中间件与静态资源目录 /static；
    2. 在 lifespan 启动阶段执行建表 + 幂等种子数据初始化；
    3. 注册全局异常处理器，把错误统一成 { code, data, msg } 契约；
    4. 汇总挂载 app/api/router.py 中的业务路由（前缀取自配置 api_prefix）。

依赖：
    - app.core.config.settings：应用名、接口前缀等运行时配置；
    - app.seed.run_seed：建表与初始化数据（幂等，可重复执行）；
    - app.api.router.api_router：auth / user / meta / weather 等业务子路由。

注意事项：
    - 响应体字段名（code/data/msg）被 H5 与小程序端共同消费，禁止随意调整；
    - 静态目录在导入阶段即创建，保证首次上传图片不因目录缺失而失败。

本地启动：
    uvicorn app.main:app --reload --port 8000
接口文档：
    http://127.0.0.1:8000/docs
"""

import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.router import api_router
from .core.config import settings
from .seed import run_seed

# 管理后台上传的图片等静态资源目录（fishing-ai-api/static）
# 目录不存在时自动创建；若部署环境只读，需要提前手工创建该目录
STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子。

    启动阶段（yield 之前）执行种子逻辑：建表 + 幂等初始化配置与演示数据；
    请求处理阶段由 yield 交出控制权；关闭阶段无需额外清理（连接池随进程释放）。

    Args:
        app: FastAPI 应用实例，由框架注入（本函数未直接使用）。
    """
    # ---- 启动阶段：建表 + 初始化数据 ----
    # 建表 + 幂等初始化数据，保证拿到代码就能直接跑
    await run_seed()
    # ---- 交出控制权，开始处理请求 ----
    yield
    # ---- 关闭阶段：无需额外清理 ----


# 应用实例：title 取自配置（展示在 /docs 顶部），lifespan 负责启动期初始化
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

# 原型（浏览器）与 H5 调试需要跨域；小程序端不受同源限制
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP 异常处理器：把 FastAPI 的 detail 也包装成统一响应体，前端只需判断 code。

    Args:
        request: 当前请求对象，仅用于框架签名，未参与逻辑。
        exc: FastAPI 抛出的 HTTPException，其 detail 作为 msg 原样返回。

    Returns:
        JSONResponse: { code: HTTP 状态码, data: None, msg: 异常描述 }。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "data": None, "msg": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """请求参数校验异常处理器：统一成 { code, data, msg }。

    pydantic 原始 errors 的 ctx 可能包含不可 JSON 序列化的对象，
    因此这里只保留「字段路径」与「错误文案」，重组成 errors 明细。

    Args:
        request: 当前请求对象，仅用于框架签名，未参与逻辑。
        exc: RequestValidationError，提供 errors() 校验明细。

    Returns:
        JSONResponse: 状态码固定 422；msg 取首条错误文案，errors 为全部明细。
    """
    # ---- 逐条提取字段名与文案（丢弃 ctx，避免序列化失败）----
    errors = []
    for e in exc.errors():
        errors.append({
            "field": ".".join(str(p) for p in e.get("loc", []) if p != "body"),
            "msg": e.get("msg", ""),
        })
    msg = errors[0]["msg"] if errors else "参数校验失败"
    # pydantic 会把自定义校验错误包成 "Value error, xxx"，去掉前缀更贴近用户文案
    msg = re.sub(r"^Value error,\s*", "", msg)
    return JSONResponse(
        status_code=422,
        content={"code": 422, "data": None, "msg": msg, "errors": errors},
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    """健康检查接口：供负载均衡 / 容器探活使用，不访问数据库。

    Returns:
        dict: 统一响应体，data.status 恒为 "ok"。
    """
    return {"code": 0, "data": {"status": "ok"}, "msg": "ok"}


# ---- 挂载业务路由：统一前缀 /api（可用环境变量 API_PREFIX 覆盖）----
app.include_router(api_router, prefix=settings.api_prefix)
