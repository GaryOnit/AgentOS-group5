"""真实路径解析和最小文件能力的外部行为测试。"""

import os
import tempfile
import unittest

from group5.security.path_capabilities import (
    PathCapabilityError,
    PathCapabilityResolver,
    normalize_linux_path,
)


class TestPathCapabilities(unittest.TestCase):
    """验证跨平台Linux规范化、目录边界和符号链接安全。"""

    def test_linux_path_normalization_is_host_independent(self) -> None:
        """Windows开发机也必须把Linux穿越路径规范化为正斜杠路径。"""
        self.assertEqual(
            normalize_linux_path("/home/user/../../etc/passwd"),
            "/etc/passwd",
        )

    def test_allowed_linux_path_gets_requested_mode(self) -> None:
        """安全根内路径应生成明确read/write能力。"""
        resolver = PathCapabilityResolver(
            allowed_roots=["/home/user/Documents"],
            protected_roots=["/etc", "/root"],
        )
        capability = resolver.resolve("/home/user/Documents/report.txt", "write")

        self.assertEqual(capability["real_path"], "/home/user/Documents/report.txt")
        self.assertEqual(capability["mode"], "write")

    def test_relative_and_prefix_trick_paths_are_rejected(self) -> None:
        """相对路径和Documents-evil前缀不得获得能力。"""
        resolver = PathCapabilityResolver(
            allowed_roots=["/home/user/Documents"],
            protected_roots=["/etc"],
        )
        with self.assertRaises(PathCapabilityError):
            resolver.resolve("../etc/passwd", "read")
        with self.assertRaises(PathCapabilityError):
            resolver.resolve("/home/user/Documents-evil/file", "write")

    def test_protected_path_is_rejected_even_if_allowed_root_is_broad(self) -> None:
        """受保护根优先级必须高于宽泛允许根。"""
        resolver = PathCapabilityResolver(
            allowed_roots=["/"],
            protected_roots=["/etc", "/root"],
        )
        with self.assertRaisesRegex(PathCapabilityError, "受保护目录"):
            resolver.resolve("/etc/passwd", "read")

    def test_argument_mapping_generates_minimum_capabilities(self) -> None:
        """source和destination应分别获得read与write能力。"""
        resolver = PathCapabilityResolver(
            allowed_roots=["/home/user"],
            protected_roots=["/etc"],
        )
        capabilities = resolver.resolve_arguments(
            {
                "source": "/home/user/Documents/a.txt",
                "destination": "/home/user/Downloads/a.txt",
                "ignored": "/etc/passwd",
            },
            {"source": "read", "destination": "write"},
        )

        self.assertEqual([item["mode"] for item in capabilities], ["read", "write"])
        self.assertEqual(len(capabilities), 2)

    def test_symlink_outside_allowed_root_is_rejected_when_supported(self) -> None:
        """允许根内指向外部目录的符号链接不得绕过真实路径检查。"""
        temp_root = os.environ.get("TEMP") or os.getcwd()
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            allowed = os.path.join(directory, "allowed")
            protected = os.path.join(directory, "protected")
            os.makedirs(allowed)
            os.makedirs(protected)
            link = os.path.join(allowed, "escape")
            try:
                os.symlink(protected, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前Windows权限不允许创建符号链接")

            resolver = PathCapabilityResolver(
                allowed_roots=[allowed],
                protected_roots=[protected],
            )
            with self.assertRaisesRegex(PathCapabilityError, "受保护目录"):
                resolver.resolve(os.path.join(link, "secret.txt"), "read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
