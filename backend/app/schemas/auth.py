from pydantic import BaseModel

from app.schemas.user import UserResponse
from app.schemas.password import Password


class LoginRequest(BaseModel):
    username: str
    password: Password


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


class RegisterRequest(BaseModel):
    username: str
    password: Password
    name: str | None = None
