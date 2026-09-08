"""一次性工具授权清单的外部行为测试。"""

import copy
import unittest

from group5.security.tool_authorization import (
    AuthorizationVerificationError,
    ToolAuthorizationIssuer,
)


def _request() -> dict:
    """返回标准工具请求。"""
    return {
        "contract_version": "1.0",
        "task_trace_id": "auth-task-001",
        "attempt_id": "auth-attempt-001",
        "tool_call_id": "auth-tool-001",
        "tool_name": "copy_file",
        "arguments": {
            "source": "/home/user/Documents/a.txt",
            "destination": "/home/user/Downloads/a.txt",
        },
    }


def _capabilities() -> list:
    """返回与测试请求相符的最小路径能力。"""
    return [
        {
            "input_path": "/home/user/Documents/a.txt",
            "normalized_path": "/home/user/Documents/a.txt",
            "real_path": "/home/user/Documents/a.txt",
            "mode": "read",
        },
        {
            "input_path": "/home/user/Downloads/a.txt",
            "normalized_path": "/home/user/Downloads/a.txt",
            "real_path": "/home/user/Downloads/a.txt",
            "mode": "write",
        },
    ]


class TestToolAuthorization(unittest.TestCase):
    """验证签发、请求绑定、篡改、过期和重放。"""

    def test_explicit_secret_is_required(self) -> None:
        """任何模式都不得隐式使用开发默认密钥。"""
        with self.assertRaisesRegex(ValueError, "显式配置"):
            ToolAuthorizationIssuer(secret=None, mode="production")

    def test_valid_manifest_is_verified_once(self) -> None:
        """合法清单应成功验证并保持工具及路径能力。"""
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())

        verified = issuer.verify_and_consume(manifest, request)
        self.assertEqual(verified["tool_name"], "copy_file")
        self.assertEqual(len(verified["path_capabilities"]), 2)
        self.assertFalse(verified["network_allowed"])

    def test_changed_arguments_do_not_match_manifest(self) -> None:
        """签发后替换destination必须被拒绝。"""
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        changed = copy.deepcopy(request)
        changed["arguments"]["destination"] = "/home/user/Other/a.txt"

        with self.assertRaisesRegex(AuthorizationVerificationError, "不匹配"):
            issuer.verify_and_consume(manifest, changed)

    def test_manifest_capability_tampering_breaks_signature(self) -> None:
        """扩大路径或网络能力会导致签名校验失败。"""
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        manifest["network_allowed"] = True

        with self.assertRaisesRegex(AuthorizationVerificationError, "签名无效"):
            issuer.verify_and_consume(manifest, request)

    def test_expired_manifest_is_rejected(self) -> None:
        """超过有效期的授权不得执行。"""
        now = [100.0]
        issuer = ToolAuthorizationIssuer(
            "test-secret",
            mode="test",
            ttl_seconds=5.0,
            clock=lambda: now[0],
        )
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        now[0] = 106.0

        with self.assertRaisesRegex(AuthorizationVerificationError, "已过期"):
            issuer.verify_and_consume(manifest, request)

    def test_consumed_manifest_cannot_be_replayed(self) -> None:
        """同一nonce第二次验证必须失败。"""
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        issuer.verify_and_consume(manifest, request)

        with self.assertRaisesRegex(AuthorizationVerificationError, "已被消费"):
            issuer.verify_and_consume(manifest, request)


if __name__ == "__main__":
    unittest.main(verbosity=2)
