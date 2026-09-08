"""跨进程工具执行的一次性签名授权清单。"""

import hashlib
import hmac
import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, TypedDict

from group5.contracts.schemas import ToolRequestV1
from group5.security.path_capabilities import PathCapability


class ToolAuthorizationManifest(TypedDict):
    """第五组签发并交给隔离工具入口验证的最小能力清单。"""

    version: str
    tool_call_id: str
    task_trace_id: str
    tool_name: str
    arguments_digest: str
    path_capabilities: List[PathCapability]
    network_allowed: bool
    expires_at: float
    nonce: str
    signature: str


class AuthorizationVerificationError(ValueError):
    """工具授权缺失、被篡改、过期或重放。"""


class ToolAuthorizationIssuer:
    """
    签发并消费一次性工具能力清单。

    production模式禁止空密钥。开发和Mock也必须由调用方显式提供测试密钥，
    本类不包含可被误用于真实运行的默认秘密。
    """

    def __init__(
        self,
        secret: Optional[str],
        mode: str = "production",
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        初始化工具授权签发器。

        Args:
            secret: HMAC密钥；production模式为空时拒绝初始化。
            mode: production、demo、test或mock。
            ttl_seconds: 授权有效秒数。
            clock: 可注入时钟，便于外部行为测试过期语义。
        """
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"production", "demo", "test", "mock"}:
            raise ValueError(f"不支持的授权模式: {mode}")
        if not isinstance(secret, str) or not secret:
            raise ValueError(f"{normalized_mode}模式必须显式配置工具授权密钥")
        if ttl_seconds <= 0:
            raise ValueError("工具授权有效期必须大于0秒")
        self.mode = normalized_mode
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._consumed_nonces = set()
        self._lock = threading.Lock()

    def issue(
        self,
        request: ToolRequestV1,
        path_capabilities: List[PathCapability],
        network_allowed: bool = False,
    ) -> ToolAuthorizationManifest:
        """
        为已经通过策略检查的工具请求签发能力清单。

        Args:
            request: 标准工具请求。
            path_capabilities: 本次调用允许的最小真实路径集合。
            network_allowed: 是否显式授予网络能力，默认False。

        Returns:
            带HMAC签名、过期时间和nonce的授权清单。
        """
        manifest: Dict[str, Any] = {
            "version": "1.0",
            "tool_call_id": request["tool_call_id"],
            "task_trace_id": request["task_trace_id"],
            "tool_name": request["tool_name"],
            "arguments_digest": _arguments_digest(request["arguments"]),
            "path_capabilities": [dict(item) for item in path_capabilities],
            "network_allowed": bool(network_allowed),
            "expires_at": self._clock() + self._ttl_seconds,
            "nonce": str(uuid.uuid4()),
        }
        manifest["signature"] = self._sign(manifest)
        return manifest  # type: ignore[return-value]

    def verify_and_consume(
        self,
        manifest: Dict[str, Any],
        request: ToolRequestV1,
    ) -> ToolAuthorizationManifest:
        """
        验证清单完整性、请求绑定、有效期并原子消费nonce。

        Args:
            manifest: 待验证授权清单。
            request: 工具入口实际准备执行的请求。

        Returns:
            通过验证的授权清单副本。

        Raises:
            AuthorizationVerificationError: 签名、绑定、过期或重放检查失败。
        """
        required = {
            "version",
            "tool_call_id",
            "task_trace_id",
            "tool_name",
            "arguments_digest",
            "path_capabilities",
            "network_allowed",
            "expires_at",
            "nonce",
            "signature",
        }
        if not isinstance(manifest, dict) or not required.issubset(manifest):
            raise AuthorizationVerificationError("工具授权清单字段不完整")
        if manifest["version"] != "1.0":
            raise AuthorizationVerificationError("工具授权版本不受支持")
        expected_signature = self._sign(
            {key: value for key, value in manifest.items() if key != "signature"}
        )
        if not hmac.compare_digest(expected_signature, str(manifest["signature"])):
            raise AuthorizationVerificationError("工具授权签名无效")
        if float(manifest["expires_at"]) <= self._clock():
            raise AuthorizationVerificationError("工具授权已过期")
        if (
            manifest["tool_call_id"] != request["tool_call_id"]
            or manifest["task_trace_id"] != request["task_trace_id"]
            or manifest["tool_name"] != request["tool_name"]
            or manifest["arguments_digest"] != _arguments_digest(request["arguments"])
        ):
            raise AuthorizationVerificationError("工具授权与实际请求不匹配")

        nonce = str(manifest["nonce"])
        with self._lock:
            if nonce in self._consumed_nonces:
                raise AuthorizationVerificationError("工具授权已被消费")
            self._consumed_nonces.add(nonce)
        return dict(manifest)  # type: ignore[return-value]

    def _sign(self, manifest_without_signature: Dict[str, Any]) -> str:
        """
        对规范化授权清单计算HMAC-SHA256。

        Args:
            manifest_without_signature: 不含signature字段的清单。

        Returns:
            十六进制HMAC摘要。
        """
        canonical = json.dumps(
            manifest_without_signature,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hmac.new(
            self._secret,
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _arguments_digest(arguments: Dict[str, Any]) -> str:
    """
    计算工具参数的稳定摘要。

    Args:
        arguments: 标准工具参数对象。

    Returns:
        SHA-256十六进制摘要。
    """
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "AuthorizationVerificationError",
    "ToolAuthorizationIssuer",
    "ToolAuthorizationManifest",
]
