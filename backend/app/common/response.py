from typing import Any

from pydantic import BaseModel


class Response(BaseModel):
    code: int
    message: str
    data: Any = None

    @classmethod
    def success(cls, data: Any = None, message: str = "请求成功"):
        return cls(code=200, message=message, data=data)

    @classmethod
    def error(cls, message: str = "请求失败", code: int = 500):
        return cls(code=code, message=message)


class PageResponse(BaseModel):
    """分页的返回结果"""

    list: Any = []
    total: int = 0
