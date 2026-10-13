from typing import Annotated

from pydantic import AfterValidator, BaseModel

from app.utils.password import validate_password_length

Password = Annotated[str, AfterValidator(validate_password_length)]


class PasswordUpdateRequest(BaseModel):
    old_password: Password
    new_password: Password
