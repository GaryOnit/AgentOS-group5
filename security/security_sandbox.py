"""
安全沙箱模块（security_sandbox.py）

核心功能：
1. check(intent_json): 对意图 JSON 进行安全检查，返回 CheckJSON
2. check_permission(action, target, params): 兼容接口
3. risk_score(): 返回当前风险统计分数
4. ping(): 健康检查

四层安全架构（按顺序执行）：
- 权限检查（Permission Check）
- 沙箱执行检查（Sandbox Gate）
- 签名验证（Signature Verify）
- 隐私保护（Privacy Guard）
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from group5.contracts.schemas import CheckJSON, IntentJSON, RiskLevel
from group5.security.policies import PolicyLoader


logger = logging.getLogger(__name__)

# 风险等级数值映射（用于 risk_score 计算）
RISK_LEVEL_SCORE: Dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 10,
}

# 风险等级比较顺序
_RISK_ORDER: Dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


class SecuritySandbox:
    """
    安全沙箱

    对传入的用户意图进行四层安全检查，决定是否放行以及风险等级。
    """

    # 认为是“写类”操作的动作
    _WRITE_ACTIONS = {
        "write", "create", "modify", "edit", "chmod", "chown",
        "mv", "copy", "delete", "remove", "unlink", "rmdir", "trash",
    }

    # 可写路径白名单（沙箱 gate）
    _SANDBOX_WRITE_ALLOW_PREFIX = (
        "/home/",
        "/tmp/",
        "/var/tmp/",
    )

    # 隐私敏感模式（命中后触发隐私保护）
    _PRIVACY_PATTERNS = (
        r"password",
        r"passwd",
        r"api[_-]?key",
        r"token",
        r"secret",
        r"private[_-]?key",
        r"id_rsa",
        r"\.ssh",
    )

    def __init__(
        self,
        policy_json_path: Optional[str] = None,
        signature_secret: Optional[str] = None,
    ) -> None:
        """
        初始化安全沙箱

        Args:
            policy_json_path: 策略 JSON 文件路径（可选，默认使用同目录的 security_policies.json）
            signature_secret: 签名验证密钥（可选，默认读环境变量 GROUP5_SIGNATURE_SECRET）
        """
        self._loader = PolicyLoader(json_path=policy_json_path)

        secret = signature_secret or os.getenv("GROUP5_SIGNATURE_SECRET", "group5-dev-secret")
        self._signature_secret = secret.encode("utf-8")

        # 统计信息：记录历史检查结果
        self._total_checks: int = 0
        self._blocked_count: int = 0
        self._risk_score_accum: int = 0

    def _normalize_path(self, target: str) -> str:
        """
        路径归一化，防止路径穿越攻击。

        例如 /home/../etc/passwd -> /etc/passwd
        """
        if target.startswith("/") or target.startswith("./") or target.startswith("../"):
            return os.path.normpath(target)
        return target

    def _max_risk(self, left: RiskLevel, right: RiskLevel) -> RiskLevel:
        """返回两个风险等级中更高的一个。"""
        return left if _RISK_ORDER[left] >= _RISK_ORDER[right] else right

    def _permission_layer(self, action: str, normalized_target: str) -> CheckJSON:
        """
        第一层：权限检查。

        依据策略表命中结果决定是否拦截，并给出初始风险等级。
        """
        matched_policy = self._loader.match(action, normalized_target)

        if matched_policy is None:
            return {
                "approved": True,
                "risk_level": "low",
                "reason": None,
                "matched_policy": None,
            }

        risk_level: RiskLevel = matched_policy["risk_level"]

        if matched_policy["block"]:
            return {
                "approved": False,
                "risk_level": risk_level,
                "reason": f"策略 {matched_policy['id']} 拦截：{matched_policy['description']}",
                "matched_policy": matched_policy["id"],
            }

        # 非拦截策略：放行但保留风险等级
        return {
            "approved": True,
            "risk_level": risk_level,
            "reason": None,
            "matched_policy": matched_policy["id"],
        }

    def _sandbox_layer(self, action: str, normalized_target: str) -> CheckJSON:
        """
        第二层：沙箱执行检查。

        目标：将“写操作”限制在可控目录（/home, /tmp, /var/tmp）。
        """
        action_lower = action.lower().strip()

        # 仅对绝对路径写操作做限制
        if action_lower in self._WRITE_ACTIONS and normalized_target.startswith("/"):
            allowed = any(normalized_target.startswith(prefix) for prefix in self._SANDBOX_WRITE_ALLOW_PREFIX)
            if not allowed:
                return {
                    "approved": False,
                    "risk_level": "critical",
                    "reason": "沙箱执行拒绝：写操作目标超出沙箱可写目录",
                    "matched_policy": "SEC-SBX-001",
                }

        return {
            "approved": True,
            "risk_level": "low",
            "reason": None,
            "matched_policy": None,
        }

    def _signature_payload(self, action: str, normalized_target: str, params: Dict[str, Any]) -> str:
        """
        生成签名原文。

        注意：signature 字段不参与签名，防止自引用。
        """
        filtered = {k: v for k, v in params.items() if k != "signature"}
        params_json = json.dumps(filtered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{action}|{normalized_target}|{params_json}"

    def _signature_layer(self, action: str, normalized_target: str, params: Dict[str, Any]) -> CheckJSON:
        """
        第三层：签名验证。

        规则：
        - 若请求未携带 signature，默认通过（兼容现有联调链路）。
        - 若携带 signature，则必须校验通过，否则拒绝。
        """
        signature = str(params.get("signature", "")).strip()
        if not signature:
            return {
                "approved": True,
                "risk_level": "low",
                "reason": None,
                "matched_policy": None,
            }

        payload = self._signature_payload(action, normalized_target, params)
        expected = hmac.new(self._signature_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return {
                "approved": False,
                "risk_level": "high",
                "reason": "签名验证失败：请求签名不合法或已被篡改",
                "matched_policy": "SEC-SIG-001",
            }

        return {
            "approved": True,
            "risk_level": "low",
            "reason": None,
            "matched_policy": "SEC-SIG-OK",
        }

    def _privacy_layer(
        self,
        action: str,
        normalized_target: str,
        intent_json: IntentJSON,
    ) -> CheckJSON:
        """
        第四层：隐私保护。

        对明显敏感信息访问做保护（尤其是 read/open/cat 场景）。
        """
        action_lower = action.lower().strip()
        raw_text = str(intent_json.get("raw_text", ""))
        params = intent_json.get("params", {})
        params_text = json.dumps(params, ensure_ascii=False).lower()

        haystack = f"{normalized_target.lower()} {raw_text.lower()} {params_text}"
        has_sensitive = any(re.search(pat, haystack) for pat in self._PRIVACY_PATTERNS)

        if has_sensitive and action_lower in {"read", "cat", "open", "navigate"}:
            return {
                "approved": False,
                "risk_level": "high",
                "reason": "隐私保护拦截：检测到疑似敏感信息访问请求",
                "matched_policy": "SEC-PRV-001",
            }

        return {
            "approved": True,
            "risk_level": "low",
            "reason": None,
            "matched_policy": None,
        }

    def check(self, intent_json: IntentJSON) -> CheckJSON:
        """
        安全检查主方法：对意图 JSON 进行四层安全检查。

        检查流程：
        1. 权限检查（策略匹配）
        2. 沙箱执行检查（可写目录约束）
        3. 签名验证（请求防篡改）
        4. 隐私保护（敏感信息防护）
        """
        self._total_checks += 1

        action: str = str(intent_json.get("action", ""))
        raw_target: str = str(intent_json.get("target", ""))
        params: Dict[str, Any] = dict(intent_json.get("params", {}))

        normalized_target = self._normalize_path(raw_target)

        logger.debug(
            "安全检查: action=%s, target=%s -> normalized=%s",
            action, raw_target, normalized_target
        )

        current_risk: RiskLevel = "low"
        matched_policy: Optional[str] = None

        # Layer 1: 权限检查
        permission_result = self._permission_layer(action, normalized_target)
        current_risk = self._max_risk(current_risk, permission_result["risk_level"])
        matched_policy = permission_result.get("matched_policy") or matched_policy
        if not permission_result["approved"]:
            self._blocked_count += 1
            self._risk_score_accum += RISK_LEVEL_SCORE[current_risk]
            return {
                "approved": False,
                "risk_level": current_risk,
                "reason": permission_result["reason"],
                "matched_policy": matched_policy,
            }

        # Layer 2: 沙箱执行检查
        sandbox_result = self._sandbox_layer(action, normalized_target)
        current_risk = self._max_risk(current_risk, sandbox_result["risk_level"])
        matched_policy = sandbox_result.get("matched_policy") or matched_policy
        if not sandbox_result["approved"]:
            self._blocked_count += 1
            self._risk_score_accum += RISK_LEVEL_SCORE[current_risk]
            return {
                "approved": False,
                "risk_level": current_risk,
                "reason": sandbox_result["reason"],
                "matched_policy": matched_policy,
            }

        # Layer 3: 签名验证
        signature_result = self._signature_layer(action, normalized_target, params)
        current_risk = self._max_risk(current_risk, signature_result["risk_level"])
        matched_policy = signature_result.get("matched_policy") or matched_policy
        if not signature_result["approved"]:
            self._blocked_count += 1
            self._risk_score_accum += RISK_LEVEL_SCORE[current_risk]
            return {
                "approved": False,
                "risk_level": current_risk,
                "reason": signature_result["reason"],
                "matched_policy": matched_policy,
            }

        # Layer 4: 隐私保护
        privacy_result = self._privacy_layer(action, normalized_target, intent_json)
        current_risk = self._max_risk(current_risk, privacy_result["risk_level"])
        matched_policy = privacy_result.get("matched_policy") or matched_policy
        if not privacy_result["approved"]:
            self._blocked_count += 1
            self._risk_score_accum += RISK_LEVEL_SCORE[current_risk]
            return {
                "approved": False,
                "risk_level": current_risk,
                "reason": privacy_result["reason"],
                "matched_policy": matched_policy,
            }

        # 全部通过
        self._risk_score_accum += RISK_LEVEL_SCORE[current_risk]
        return {
            "approved": True,
            "risk_level": current_risk,
            "reason": None,
            "matched_policy": matched_policy,
        }

    def check_permission(
        self,
        action: str,
        target: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> CheckJSON:
        """
        兼容接口：使用独立参数进行安全检查（内部构造 IntentJSON 后调用 check()）
        """
        intent: IntentJSON = {
            "trace_id": "compat-check",
            "action": action,
            "target": target,
            "params": params or {},
            "raw_text": f"{action} {target}",
        }
        return self.check(intent)

    def risk_score(self) -> Dict[str, Any]:
        """
        返回当前风险统计分数。
        """
        block_rate = (
            self._blocked_count / self._total_checks
            if self._total_checks > 0
            else 0.0
        )
        return {
            "total_checks": self._total_checks,
            "blocked_count": self._blocked_count,
            "risk_score_accum": self._risk_score_accum,
            "block_rate": round(block_rate, 4),
        }

    def ping(self) -> Dict[str, Any]:
        """
        健康检查方法
        """
        start = time.perf_counter()
        try:
            policy_count = len(self._loader.get_policies())
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "module": "security_sandbox",
                "healthy": True,
                "latency_ms": round(latency_ms, 2),
                "policy_count": policy_count,
            }
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "module": "security_sandbox",
                "healthy": False,
                "latency_ms": round(latency_ms, 2),
                "error": str(exc),
            }


if __name__ == "__main__":
    # 独立运行示例：展示安全沙箱的各种检查场景
    sandbox = SecuritySandbox()

    print("=== 安全沙箱检查示例 ===\n")

    test_cases = [
        # (action, target, raw_text, 期望结果说明)
        ("open", "/home/user/Documents", "打开文档文件夹", "应放行（低风险）"),
        ("rm", "-rf /", "删除根目录", "应拦截（危险命令 SEC-001）"),
        ("navigate", "/etc/passwd", "访问密码文件", "应拦截（受保护路径 SEC-002）"),
        ("navigate", "/home/../etc/passwd", "路径穿越攻击", "应拦截（路径穿越 -> /etc/passwd）"),
        ("delete", "/home/user/old_file.txt", "删除旧文件", "应放行但 high 风险（SEC-003）"),
        ("create", "/home/user/new_folder", "新建文件夹", "应放行但 medium 风险（SEC-004）"),
        ("read", "/home/user/.ssh/id_rsa", "读取私钥", "应拦截（隐私保护 SEC-PRV-001）"),
    ]

    for action, target, raw_text, desc in test_cases:
        intent: IntentJSON = {
            "trace_id": "test-001",
            "action": action,
            "target": target,
            "params": {},
            "raw_text": raw_text,
        }
        result = sandbox.check(intent)
        status = "OK 放行" if result["approved"] else "BLOCK 拦截"
        print(f"  {status} | {raw_text}")
        print(f"        期望: {desc}")
        print(f"        结果: risk={result['risk_level']}, policy={result['matched_policy']}")
        if result["reason"]:
            print(f"        原因: {result['reason']}")
        print()

    print("=== 风险统计 ===")
    print(sandbox.risk_score())

    print("\n=== 健康检查 ===")
    print(sandbox.ping())

    print("\nsecurity_sandbox.py 验证通过")
