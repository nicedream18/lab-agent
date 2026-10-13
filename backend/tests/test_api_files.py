"""文件上传接口回归：扩展名、大小提示、配置覆盖与落盘。"""

from __future__ import annotations

import io
import re
import unittest
from pathlib import Path

import app.models  # noqa: F401
from tests.api_harness import ApiTestCase
from starlette.datastructures import UploadFile

from app.api import files as files_module
from app import config
from app.common.exceptions import BusinessException

UPLOAD = "/api/files/upload"
DISK_NAME_RE = re.compile(r"^\d{13}_[0-9a-f]{8}\.[a-z0-9]+$")


class UploadCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 把落盘目录换成本次用例的临时目录，绝不往项目 uploads/ 里写垃圾
        self.upload_dir = Path(self._tmpdir.name) / "uploads"
        self.upload_dir.mkdir()
        self._patch(config, "UPLOAD_DIR", self.upload_dir)

    def _post(self, filename: str, content: bytes, content_type: str = "image/png"):
        return self.client.post(
            UPLOAD, files={"file": (filename, io.BytesIO(content), content_type)}
        )

    # ------------------------------------------------------------------
    # 正常路径
    # ------------------------------------------------------------------

    def test_upload_writes_the_file_and_returns_its_metadata(self) -> None:
        data = self.assert_success(self._post("头像.png", b"\x89PNG-hello"))

        self.assertEqual(data["original_name"], "头像.png")
        self.assertTrue(DISK_NAME_RE.match(data["disk_name"]), data["disk_name"])
        self.assertTrue(data["disk_name"].endswith(".png"))
        self.assertEqual(data["size"], len(b"\x89PNG-hello"))
        self.assertEqual(data["url"], f"/uploads/{data['disk_name']}")

        saved = self.upload_dir / data["disk_name"]
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), b"\x89PNG-hello")

    def test_upload_does_not_need_a_token(self) -> None:
        """上传是公开接口（登录页换头像也要用）。"""
        self.assert_success(self._post("a.png", b"x"))

    def test_the_disk_name_is_not_the_client_name(self) -> None:
        """落盘名是时间戳 + 随机串，客户端改不了自己的存储路径。"""
        first = self.assert_success(self._post("same.png", b"1"))["disk_name"]
        second = self.assert_success(self._post("same.png", b"2"))["disk_name"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.upload_dir.iterdir())), 2)

    def test_directory_prefix_is_stripped_from_the_client_filename(self) -> None:
        """做一个「文件名叫 ../../etc/passwd」的客户端也不能写到目录外面去。"""
        data = self.assert_success(self._post("../../etc/evil.PNG", b"x"))
        self.assertEqual(data["original_name"], "evil.PNG")
        self.assertEqual(len(list(self.upload_dir.iterdir())), 1)

    def test_the_extension_is_lowercased(self) -> None:
        data = self.assert_success(self._post("PHOTO.JPEG", b"x"))
        self.assertTrue(data["disk_name"].endswith(".jpeg"), data["disk_name"])

    def test_every_allowed_extension_is_accepted(self) -> None:
        for ext in sorted(config.ALLOWED_EXTENSIONS):
            with self.subTest(ext=ext):
                data = self.assert_success(self._post(f"f{ext}", b"x"))
                self.assertTrue(data["disk_name"].endswith(ext))

    # ------------------------------------------------------------------
    # 拒绝路径
    # ------------------------------------------------------------------

    def test_a_disallowed_extension_is_rejected(self) -> None:
        response = self._post("notes.txt", b"hello")
        self.assert_business_error(response, "不支持的文件后缀：.txt")
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_a_filename_without_any_extension_is_rejected(self) -> None:
        response = self._post("README", b"x")
        self.assert_business_error(response, "不支持的文件后缀：")

    def test_a_double_extension_only_looks_at_the_last_one(self) -> None:
        """`shell.php.png` 的 suffix 是 `.png`，能通过 —— 这里不做黑白名单扫描。"""
        data = self.assert_success(self._post("shell.php.png", b"x"))
        self.assertEqual(data["original_name"], "shell.php.png")
        self.assertTrue(data["disk_name"].endswith(".png"))

    def test_an_oversized_file_is_rejected(self) -> None:
        self._patch(config, "MAX_FILE_SIZE", 3)
        response = self._post("big.png", b"hello")
        self.assert_business_error(response, "文件不能超过 3B")
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_the_oversize_message_uses_a_readable_unit(self) -> None:
        self._patch(config, "MAX_FILE_SIZE", 100 * 1024)
        response = self._post("big.png", b"x" * (100 * 1024 + 1))
        self.assert_business_error(response, "文件不能超过 100KB")

        self._patch(config, "MAX_FILE_SIZE", 3 * 1024 * 1024)
        response = self._post("big.png", b"x" * (3 * 1024 * 1024 + 1))
        self.assert_business_error(response, "文件不能超过 3MB")

    def test_a_file_exactly_at_the_limit_is_accepted(self) -> None:
        self._patch(config, "MAX_FILE_SIZE", 10)
        data = self.assert_success(self._post("edge.png", b"x" * 10))
        self.assertEqual(data["size"], 10)

    # ------------------------------------------------------------------
    # 直接调用：覆盖 HTTP 到不了的分支
    # ------------------------------------------------------------------

    def _call_handler(self, filename: str, content: bytes = b"x") -> None:
        files_module.upload(UploadFile(file=io.BytesIO(content), filename=filename))

    def test_an_empty_filename_is_rejected_by_the_handler_itself(self) -> None:
        """HTTP 走不到这条分支：FastAPI 会先把空 filename 判成「文件字段缺失」→ 422。

        所以只能直接调用处理函数来覆盖它。留着这条测试的意义是：
        守卫本身是有效的（不是死代码逻辑错误），只是从网络上永远走不到。
        """
        with self.assertRaises(BusinessException) as ctx:
            self._call_handler("")
        self.assertEqual(ctx.exception.message, "文件名不能为空")

    def test_an_empty_filename_over_http_becomes_a_422_instead(self) -> None:
        response = self.client.post(
            UPLOAD, files={"file": ("", io.BytesIO(b"x"), "image/png")}
        )
        self.assert_validation_error(response)
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_a_missing_file_field_is_a_422(self) -> None:
        self.assert_validation_error(self.client.post(UPLOAD))

    def test_the_handler_does_not_depend_on_upload_dir_existing(self) -> None:
        """目录不存在时不是优雅报错，而是直接抛 FileNotFoundError → 500。

        顺便记录一个部署前提：`app/config.py` 在 import 时就会 mkdir，
        所以正常启动流程里这个目录一定在。
        """
        self._patch(config, "UPLOAD_DIR", self.upload_dir / "not-created-yet")
        with self.assertRaises(FileNotFoundError):
            self._call_handler("a.png")


if __name__ == "__main__":
    unittest.main()
