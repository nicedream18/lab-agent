import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, UploadFile

from app.common.exceptions import BusinessException
from app.common.response import Response
from app import config

router = APIRouter(prefix="/files", tags=["文件管理"])


@router.post("/upload")
def upload(file: UploadFile = File(...)):  # noqa: B008
    """文件上传的接口"""
    if not file.filename:
        raise BusinessException(message="文件名不能为空")

    # 原始的文件名  用户头像.jpg
    original_name = os.path.basename(file.filename)
    # 文件后缀 .jpg
    ext = Path(original_name).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise BusinessException(message=f"不支持的文件后缀：{ext}")
    # 文件大小校验
    if file.size and file.size > config.MAX_FILE_SIZE:
        limit = config.MAX_FILE_SIZE
        display_limit = (
            f"{limit // (1024 * 1024)}MB"
            if limit >= 1024 * 1024 and limit % (1024 * 1024) == 0
            else f"{limit / 1024:g}KB" if limit >= 1024
            else f"{limit}B"
        )
        raise BusinessException(
            message=f"文件不能超过 {display_limit}"
        )
    # 设置唯一的文件名称   199323213213_ddadaqwerq.jpg
    disk_name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
    # 文件存储的实际路径
    save_path = config.UPLOAD_DIR / disk_name

    # 流式写文件
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return Response.success(
        data={
            "original_name": original_name,
            "disk_name": disk_name,
            "size": file.size,
            "url": f"/uploads/{disk_name}",
        }
    )
