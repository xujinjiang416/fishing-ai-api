"""main.py — FastAPI 应用入口

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
STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表 + 幂等初始化数据，保证拿到代码就能直接跑
    await run_seed()
    yield


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
    """把 FastAPI 的 detail 也包装成统一响应体，前端只需判断 code。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "data": None, "msg": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """统一成 { code, data, msg }；只取字段名与文案（pydantic 原始 errors 含不可序列化的 ctx）。"""
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
    return {"code": 0, "data": {"status": "ok"}, "msg": "ok"}


app.include_router(api_router, prefix=settings.api_prefix)
