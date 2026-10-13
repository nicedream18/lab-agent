import bcrypt

MAX_PASSWORD_BYTES = 72


def validate_password_length(password: str) -> str:
    """bcrypt 只接受最多 72 个 UTF-8 字节。"""
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError("密码不能超过 72 字节")
    return password


def hash_password(password: str) -> str:
    """对密码加密"""
    return bcrypt.hashpw(validate_password_length(password).encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    """验证密码的有效性"""
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False
