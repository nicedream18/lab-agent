from typing import cast

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.common.response import Response


class BusinessException(Exception):
    """自定义的业务异常"""

    def __init__(self, message: str, code: int = 500):
        self.message = message
        self.code = code
        super().__init__(message)


async def bussiness_excpetion_hadler(_: Request, exc: Exception):
    """自定义业务异常处理器"""
    # starlette 的 ExceptionHandler 把第二个参数声明为 Exception，而函数参数是
    # 逆变的：声明成 BusinessException 会被判为不兼容。FastAPI 是按注册的异常
    # 类型分派的，走到这里的一定是 BusinessException，这里如实断言。
    biz = cast(BusinessException, exc)
    return JSONResponse(
        status_code=200,
        content=Response.error(code=biz.code, message=biz.message).model_dump(),
    )


async def http_excpetion_hadler(_: Request, exc: Exception):
    """Http异常处理器"""
    # 同上：按注册类型分派，这里必定是 HTTPException。
    http_exc = cast(HTTPException, exc)
    return JSONResponse(
        status_code=http_exc.status_code,
        content=Response.error(
            code=http_exc.status_code, message=http_exc.detail
        ).model_dump(),
    )


async def validation_excpetion_hadler(_: Request, exc: Exception):
    """参数异常处理器"""
    del exc  # 参数由 FastAPI 传入；这里只用固定文案，不需要异常对象
    return JSONResponse(
        status_code=422,
        content=Response.error(code=422, message="请求参数校验错误").model_dump(),
    )


async def global_excpetion_hadler(_: Request, exc: Exception):
    """全局异常处理器"""
    del exc  # 参数由 FastAPI 传入；这里只用固定文案，不需要异常对象
    return JSONResponse(
        status_code=500,
        content=Response.error(code=500, message="服务器内部错误").model_dump(),
    )
