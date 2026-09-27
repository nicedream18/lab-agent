import bcrypt


def hash_password(password: str) -> str:
    """对密码加密"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    """验证密码的有效性"""
    """  "123" -> "xxsdasdasdasfa"  """
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
