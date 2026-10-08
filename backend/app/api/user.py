from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.response import Response
from app.database import get_db
from app.dependencies.auth import get_current_admin, get_current_user
from app.models.user import User
from app.schemas.password import PasswordUpdateRequest
from app.schemas.user import UserCreateRequest, UserResponse, UserUpdateRequest
from app.services import user_service

router = APIRouter(prefix="/user", tags=["用户信息接口"])


# 获取当前登录用户信息
@router.get("/info")
def get_user_info(current_user: User = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return Response.success(data=UserResponse.model_validate(current_user))


# 更新当前登录用户信息
@router.put("/update")
def update_user_info(
    data: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新当前用户信息"""
    res = user_service.update_user_info(db, current_user, data)
    return Response.success(data=res)


# 更新当前登录用户密码
@router.put("/password")
def update_user_password(
    data: PasswordUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新当前用户密码"""
    user_service.update_user_password(db, current_user, data)
    return Response.success(message="密码修改成功")


# 管理员接口 获取成员列表
@router.get("/list")
def get_user_list(
    page: int = 1,
    page_size: int = 10,
    keywords: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    res = user_service.get_user_page_list(db, page, page_size, keywords)
    return Response.success(data=res)


# 管理员接口 创建用户
@router.post("")
def create_user(
    data: UserCreateRequest,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    res = user_service.create_user(db, data)
    return Response.success(data=res)


# 管理员接口 更新用户
@router.put("/{user_id}")
def create_user(
    user_id: int,
    data: UserUpdateRequest,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    res = user_service.update_user(db, user_id, data)
    return Response.success(data=res)


# 管理员接口 删除用户
@router.delete("/{user_id}")
def create_user(
    user_id: int,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    res = user_service.delete_user(db, user_id, current_user)
    return Response.success()
