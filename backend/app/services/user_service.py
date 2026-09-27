import keyword
from operator import itemgetter, or_

from fastapi import Query

from app.common.exceptions import BusinessException
from app.common.response import PageResponse
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdateRequest
from sqlalchemy.orm import Session

from app.utils.password import verify_password, hash_password
from app.schemas.password import PasswordUpdateRequest


def get_user_info(user: User) -> UserResponse:
    return UserResponse.model_validate(user)


def update_user_info(db: Session, user: User, data: UserUpdateRequest):
    # pydatic对象转换成字典
    user_dict = data.model_dump(
        exclude_none=True, exclude={"role", "status", "password"}
    )
    for field, value in user_dict.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


def update_user_password(db: Session, user: User, data: PasswordUpdateRequest):
    if not verify_password(data.old_password, user.password):
        raise BusinessException("旧密码不正确")
    if data.old_password == data.new_password:
        raise BusinessException("新密码不能与旧密码相同")
    user.password = hash_password(data.new_password)  # 必须加密后入库
    db.commit()


def get_user_page_list(db: Session, page: int, page_size: int, keyword: str = None):
    """获取用户分页列表"""
    query = db.query(User)
    if keyword:
        query = query.filter(
            or_(User.username.ilike(f"%{keyword}%"), User.name.ilike(f"%{keyword}%"))
        )
    total = query.count()
    items = (
        query.order_by(User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return PageResponse(
        list=[UserResponse.model_validate(u) for u in items], total=total
    )


def create_user(db: Session, data: UserUpdateRequest):
    """创建用户"""
    # 检查用户名是否已存在
    existing_user = db.query(User).filter(User.username == data.username).first()
    if existing_user:
        raise BusinessException("用户名已存在")
    # 创建新用户
    new_user = User(
        username=data.username,
        password=hash_password(data.password),  # 密码加密
        name=data.name,
        email=data.email,
        phone=data.phone,
        avatar=data.avatar,
        role=data.role,
        status=data.status,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return UserResponse.model_validate(new_user)


def update_user(db: Session, user_id: int, data: UserUpdateRequest):
    """更新用户信息"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise BusinessException("用户不存在")
    # 更新用户信息
    user_dict = data.model_dump(exclude_none=True)
    for field, value in user_dict.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


def delete_user(db: Session, user_id: int, currut_user: User):
    """删除用户"""

    if user_id == currut_user.id:
        raise BusinessException(message="不能删除当前登录的账号")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise BusinessException(message="用户不存在")
    db.delete(user)
    db.commit()
