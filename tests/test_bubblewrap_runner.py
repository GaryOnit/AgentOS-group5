"""Bubblewrap运行器失败关闭、环境隔离和命令构造测试。"""

import subprocess
import sys
import unittest
from types import SimpleNamespace

from group5.security.bubblewrap_runner import BubblewrapRunner, SandboxExecutionError
from group5.security.tool_authorization import ToolAuthorizationIssuer


def _request() -> dict:
    """返回测试工具请求。"""
    return {
        "contract_version": "1.0",
        "task_trace_id": "sandbox-task",
        "attempt_id": "sandbox-attempt",
        "tool_call_id": "sandbox-call",
        "tool_name": "copy_file",
        "arguments": {"source": "/tmp/a", "destination": "/tmp/b"},
    }


def _capabilities() -> list:
    """返回一读一写路径能力。"""
    return [
        {
            "input_path": "/tmp/a",
            "normalized_path": "/tmp/a",
            "real_path": "/tmp/a",
            "mode": "read",
        },
        {
            "input_path": "/tmp/b",
            "normalized_path": "/tmp/b",
            "real_path": "/tmp/b",
            "mode": "write",
        },
    ]


class _RecordingExecutor:
    """记录subprocess.run公开调用参数的测试执行器。"""

    def __init__(self, result=None, error=None) -> None:
        """保存预设结果或异常。"""
        self.calls = []
        self.result = result or SimpleNamespace(returncode=0, stdout="ok", stderr="")
        self.error = error

    def __call__(self, command, **kwargs):
        """记录调用并返回预设结果。"""
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class TestBubblewrapRunner(unittest.TestCase):
    """验证授权消费、Mock标记、失败关闭和最小环境。"""

    def test_missing_bubblewrap_fails_closed(self) -> None:
        """production模式下不存在bwrap时不得调用宿主执行器。"""
        executor = _RecordingExecutor()
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        runner = BubblewrapRunner(
            mode="production",
            bwrap_path="D:\\missing\\bwrap.exe",
            executor=executor,
        )

        with self.assertRaisesRegex(SandboxExecutionError, "拒绝执行真实工具"):
            runner.run(request, manifest, issuer, ["copy-tool"], timeout=1)
        self.assertEqual(executor.calls, [])

    def test_mock_mode_never_calls_executor(self) -> None:
        """Mock模式必须明确返回executed=False。"""
        executor = _RecordingExecutor()
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        runner = BubblewrapRunner(mode="mock", executor=executor)

        result = runner.run(request, manifest, issuer, ["copy-tool"], timeout=1)
        self.assertTrue(result["success"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["sandbox_mode"], "mock")
        self.assertEqual(executor.calls, [])

    def test_command_uses_mount_modes_network_isolation_and_clean_env(self) -> None:
        """真实模式参数应区分只读/写挂载并默认不共享网络。"""
        executor = _RecordingExecutor()
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        runner = BubblewrapRunner(
            mode="test",
            bwrap_path=sys.executable,
            executor=executor,
        )

        result = runner.run(request, manifest, issuer, ["copy-tool"], timeout=2)
        command, kwargs = executor.calls[0]
        self.assertTrue(result["success"])
        self.assertIn("--unshare-all", command)
        self.assertNotIn("--share-net", command)
        self.assertIn("--ro-bind", command)
        self.assertIn("--bind", command)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])

    def test_explicit_network_capability_adds_share_net(self) -> None:
        """只有签名清单明确授权时才能共享宿主网络。"""
        executor = _RecordingExecutor()
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities(), network_allowed=True)
        runner = BubblewrapRunner(
            mode="test",
            bwrap_path=sys.executable,
            executor=executor,
        )

        runner.run(request, manifest, issuer, ["network-tool"], timeout=2)
        self.assertIn("--share-net", executor.calls[0][0])

    def test_timeout_returns_sandbox_error(self) -> None:
        """进程超时应返回稳定sandbox_timeout错误。"""
        executor = _RecordingExecutor(
            error=subprocess.TimeoutExpired(cmd="tool", timeout=0.1)
        )
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        runner = BubblewrapRunner(
            mode="test",
            bwrap_path=sys.executable,
            executor=executor,
        )

        with self.assertRaises(SandboxExecutionError) as raised:
            runner.run(request, manifest, issuer, ["slow-tool"], timeout=0.1)
        self.assertEqual(raised.exception.code, "sandbox_timeout")

    def test_tool_output_is_redacted_and_truncated(self) -> None:
        """工具输出中的密钥不得进入标准结果。"""
        executor = _RecordingExecutor(
            result=SimpleNamespace(
                returncode=0,
                stdout="sk-1234567890abcdef " + "x" * 500,
                stderr="",
            )
        )
        issuer = ToolAuthorizationIssuer("test-secret", mode="test")
        request = _request()
        manifest = issuer.issue(request, _capabilities())
        runner = BubblewrapRunner(
            mode="test",
            bwrap_path=sys.executable,
            executor=executor,
            max_output_chars=256,
        )

        result = runner.run(request, manifest, issuer, ["tool"], timeout=1)
        self.assertNotIn("sk-1234567890abcdef", result["stdout"])
        self.assertLessEqual(len(result["stdout"]), 256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
