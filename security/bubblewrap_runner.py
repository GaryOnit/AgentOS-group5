"""基于Bubblewrap的一次性授权工具进程运行器。"""

import os
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional

from group5.contracts.schemas import ToolRequestV1
from group5.security.path_capabilities import PathCapabilityResolver
from group5.security.redaction import redact
from group5.security.tool_authorization import (
    AuthorizationVerificationError,
    ToolAuthorizationIssuer,
)


class SandboxExecutionError(RuntimeError):
    """
    Bubblewrap不可用、授权无效或沙箱执行失败。

    Args:
        code: 稳定的沙箱错误标识。
        message: 不包含秘密的错误说明。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class BubblewrapRunner:
    """
    使用最小挂载、默认断网和清空环境运行第四组工具命令。

    工具授权在启动进程前由签发器验证并消费。production模式下Bubblewrap
    缺失时失败关闭；mock模式只返回明确的未执行结果。
    """

    def __init__(
        self,
        mode: str = "production",
        bwrap_path: Optional[str] = None,
        executor: Callable[..., Any] = subprocess.run,
        max_output_chars: int = 8192,
    ) -> None:
        """
        初始化Bubblewrap运行器。

        Args:
            mode: production、demo、test或mock。
            bwrap_path: 可选Bubblewrap绝对路径；为空时从PATH只读查找。
            executor: 可注入的进程执行函数，便于无副作用测试。
            max_output_chars: stdout和stderr最大保留字符数。
        """
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"production", "demo", "test", "mock"}:
            raise ValueError(f"不支持的沙箱模式: {mode}")
        self.mode = normalized_mode
        self.bwrap_path = bwrap_path or shutil.which("bwrap")
        self._executor = executor
        self._max_output_chars = max(int(max_output_chars), 256)

    def ping(self) -> Dict[str, Any]:
        """
        返回沙箱运行器可用性。

        Returns:
            明确区分真实Bubblewrap和mock模式的健康状态。
        """
        available = bool(self.bwrap_path and os.path.isfile(self.bwrap_path))
        if self.mode == "mock":
            return {
                "healthy": True,
                "available": available,
                "mode": "mock",
                "detail": "mock模式不会执行真实系统命令",
            }
        return {
            "healthy": available,
            "available": available,
            "mode": self.mode,
            "detail": None if available else "Bubblewrap不可用，变更工具将失败关闭",
        }

    def run(
        self,
        request: ToolRequestV1,
        manifest: Dict[str, Any],
        issuer: ToolAuthorizationIssuer,
        command: List[str],
        timeout: float,
        path_resolver: Optional[PathCapabilityResolver] = None,
    ) -> Dict[str, Any]:
        """
        验证授权并在Bubblewrap中运行参数数组命令。

        Args:
            request: 实际准备执行的标准工具请求。
            manifest: 第五组签发的工具能力清单。
            issuer: 用于验证并消费清单的签发器。
            command: 不经过shell解析的可执行文件与参数数组。
            timeout: 工具进程超时秒数。
            path_resolver: 可选路径解析器，用于执行前复检真实目标。

        Returns:
            标准化沙箱执行结果。

        Raises:
            SandboxExecutionError: 授权、环境、超时或进程执行失败。
        """
        if not isinstance(command, list) or not command or any(
            not isinstance(part, str) or not part for part in command
        ):
            raise SandboxExecutionError("invalid_command", "工具命令必须是非空字符串数组")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise SandboxExecutionError("invalid_timeout", "工具超时必须大于0秒")

        try:
            verified = issuer.verify_and_consume(manifest, request)
        except AuthorizationVerificationError as exc:
            raise SandboxExecutionError("authorization_invalid", str(exc)) from exc

        if path_resolver is not None:
            for capability in verified["path_capabilities"]:
                path_resolver.revalidate(capability)

        if self.mode == "mock":
            return {
                "success": True,
                "executed": False,
                "sandbox_mode": "mock",
                "tool_call_id": request["tool_call_id"],
                "output": "Mock模式未执行真实工具",
            }
        if not self.bwrap_path or not os.path.isfile(self.bwrap_path):
            raise SandboxExecutionError(
                "sandbox_unavailable",
                "Bubblewrap不可用，拒绝执行真实工具",
            )

        sandbox_command = self._build_command(verified, command)
        # 宿主进程只向bwrap传递启动所需的最小环境；bwrap内部再次使用
        # --clearenv，防止模型Key、签名密钥和用户令牌进入工具进程。
        process_environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        try:
            completed = self._executor(
                sandbox_command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=float(timeout),
                env=process_environment,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxExecutionError("sandbox_timeout", "隔离工具执行超时") from exc
        except OSError as exc:
            raise SandboxExecutionError(
                "sandbox_start_failed",
                f"Bubblewrap启动失败: {type(exc).__name__}",
            ) from exc

        stdout = str(redact(str(getattr(completed, "stdout", ""))))
        stderr = str(redact(str(getattr(completed, "stderr", ""))))
        return_code = int(getattr(completed, "returncode", 1))
        return {
            "success": return_code == 0,
            "executed": True,
            "sandbox_mode": "bubblewrap",
            "tool_call_id": request["tool_call_id"],
            "return_code": return_code,
            "stdout": stdout[: self._max_output_chars],
            "stderr": stderr[: self._max_output_chars],
        }

    def _build_command(
        self,
        manifest: Dict[str, Any],
        command: List[str],
    ) -> List[str]:
        """
        构造不经过shell插值的Bubblewrap参数数组。

        Args:
            manifest: 已验证工具能力清单。
            command: 工具可执行文件与参数。

        Returns:
            可直接交给subprocess.run的参数数组。
        """
        result = [
            str(self.bwrap_path),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
        ]
        if bool(manifest.get("network_allowed", False)):
            result.append("--share-net")
        result.extend(["--proc", "/proc", "--tmpfs", "/tmp", "--clearenv"])
        result.extend(["--setenv", "PATH", "/usr/bin:/bin"])
        result.extend(["--setenv", "LANG", "C.UTF-8"])

        # 仅提供工具启动所需的常见只读运行时目录；不存在的目录跳过，
        # 具体Linux发行版差异由集成测试验证。
        for system_path in ("/usr", "/bin", "/lib", "/lib64"):
            if os.path.exists(system_path):
                result.extend(["--ro-bind", system_path, system_path])

        for capability in manifest.get("path_capabilities", []):
            real_path = str(capability["real_path"])
            bind_flag = "--bind" if capability["mode"] == "write" else "--ro-bind"
            result.extend([bind_flag, real_path, real_path])
        result.append("--")
        result.extend(command)
        return result


__all__ = ["BubblewrapRunner", "SandboxExecutionError"]
