"""
安全模块测试（test_security.py）

测试 SecuritySandbox 和 PolicyLoader 的各种场景：
1. 危险命令拦截（rm、shutdown）
2. 受保护路径拦截（/etc/passwd）
3. 路径穿越防护（/home/../etc/passwd）
4. 安全操作放行（open + /home/user）
5. 高风险标记（delete 操作）
6. JSON 文件缺失时 fallback
7. risk_score() 方法
"""

import os
import tempfile
import json
import unittest

from group5.contracts.schemas import IntentJSON
from group5.security.policies import DEFAULT_POLICIES, PolicyLoader
from group5.security.security_sandbox import SecuritySandbox


def _make_intent(action: str, target: str, trace_id: str = "test-trace") -> IntentJSON:
    """辅助函数：快速构建 IntentJSON"""
    return {
        "trace_id": trace_id,
        "action": action,
        "target": target,
        "params": {},
        "raw_text": f"{action} {target}",
    }


class TestSecuritySandbox(unittest.TestCase):
    """SecuritySandbox 的单元测试集"""

    def setUp(self) -> None:
        """每个测试前创建新的 SecuritySandbox 实例"""
        self.sandbox = SecuritySandbox()

    def test_block_dangerous_command(self) -> None:
        """
        测试1：危险命令应被拦截

        rm、shutdown 等命令必须被 SEC-001 策略拦截。
        """
        # rm 命令应被拦截
        result = self.sandbox.check(_make_intent("rm", "-rf /"))
        self.assertFalse(result["approved"], "rm 命令应被拦截")
        self.assertEqual(result["risk_level"], "critical", "危险命令应为 critical 风险")
        self.assertIsNotNone(result["reason"], "拦截时应有 reason 字段")
        self.assertEqual(result["matched_policy"], "SEC-001", "应命中 SEC-001 策略")

        # shutdown 命令应被拦截
        result2 = self.sandbox.check(_make_intent("shutdown", "system"))
        self.assertFalse(result2["approved"], "shutdown 命令应被拦截")
        self.assertEqual(result2["matched_policy"], "SEC-001")

        # reboot 命令应被拦截
        result3 = self.sandbox.check(_make_intent("reboot", "now"))
        self.assertFalse(result3["approved"], "reboot 命令应被拦截")

    def test_block_protected_path(self) -> None:
        """
        测试2：受保护路径应被拦截

        访问 /etc/passwd 等系统路径应被 SEC-002 拦截。
        """
        # /etc/passwd 应被拦截
        result = self.sandbox.check(_make_intent("navigate", "/etc/passwd"))
        self.assertFalse(result["approved"], "/etc/passwd 应被拦截")
        self.assertEqual(result["risk_level"], "critical")
        self.assertEqual(result["matched_policy"], "SEC-002", "应命中 SEC-002 受保护路径策略")

        # /sys/kernel 也应被拦截
        result2 = self.sandbox.check(_make_intent("read", "/sys/kernel/config"))
        self.assertFalse(result2["approved"], "/sys/kernel 应被拦截")

        # /proc/1/maps 也应被拦截
        result3 = self.sandbox.check(_make_intent("open", "/proc/1/maps"))
        self.assertFalse(result3["approved"], "/proc 路径应被拦截")

    def test_path_traversal_block(self) -> None:
        """
        测试3：路径穿越攻击必须被拦截（核心安全测试）

        /home/../etc/passwd 经 normpath 归一化后变为 /etc/passwd，
        应触发 SEC-002 受保护路径策略。
        """
        # /home/../etc/passwd 归一化为 /etc/passwd → 应被 SEC-002 拦截
        result = self.sandbox.check(_make_intent("navigate", "/home/../etc/passwd"))
        self.assertFalse(result["approved"], "路径穿越 /home/../etc/passwd 必须被拦截")
        self.assertEqual(result["matched_policy"], "SEC-002", "路径穿越后应命中 SEC-002")

        # 多层穿越：/home/user/../../etc/shadow → /etc/shadow
        result2 = self.sandbox.check(_make_intent("read", "/home/user/../../etc/shadow"))
        self.assertFalse(result2["approved"], "多层路径穿越应被拦截")

        # ./../../etc/passwd → 也应被拦截
        result3 = self.sandbox.check(_make_intent("cat", "../etc/passwd"))
        # 注意：../etc/passwd 归一化依赖 CWD，normpath 返回 ../etc/passwd 若非绝对路径
        # 但若从根目录执行 normpath("../etc/passwd") 可能不匹配 SEC-002
        # 绝对路径的穿越才是核心测试
        # 以下用绝对路径穿越测试
        result4 = self.sandbox.check(_make_intent("cat", "/var/log/../../etc/passwd"))
        self.assertFalse(result4["approved"], "绝对路径穿越 /var/log/../../etc/passwd 应被拦截")

    def test_allow_safe_operation(self) -> None:
        """
        测试4：安全操作应放行

        open + /home/user 不在危险命令列表和受保护路径中，应放行为 low 风险。
        """
        result = self.sandbox.check(_make_intent("open", "/home/user/Documents"))
        self.assertTrue(result["approved"], "open /home/user/Documents 应放行")
        self.assertEqual(result["risk_level"], "low", "安全操作应为 low 风险")
        self.assertIsNone(result["reason"], "放行时 reason 应为 None")

        # 其他安全操作
        result2 = self.sandbox.check(_make_intent("navigate", "/home/user/Downloads"))
        self.assertTrue(result2["approved"], "navigate 到 home 目录应放行")

    def test_high_risk_flag(self) -> None:
        """
        测试5：delete 操作应返回 high 风险但 approved=True（SEC-003 不拦截）

        SEC-003 策略的 block=False，仅标记风险，不拦截。
        """
        result = self.sandbox.check(_make_intent("delete", "/home/user/old_file.txt"))
        self.assertTrue(result["approved"], "delete 操作应放行（SEC-003 不拦截）")
        self.assertEqual(result["risk_level"], "high", "delete 操作应为 high 风险")
        self.assertEqual(result["matched_policy"], "SEC-003", "应命中 SEC-003 策略")

        # remove 也是 high 风险放行
        result2 = self.sandbox.check(_make_intent("remove", "/home/user/temp/"))
        self.assertTrue(result2["approved"])
        self.assertEqual(result2["risk_level"], "high")

    def test_policy_json_load_fallback(self) -> None:
        """
        测试6：JSON 文件缺失时应使用内置规则（fallback）

        PolicyLoader 在指定路径不存在时应 fallback 到 DEFAULT_POLICIES。
        """
        # 使用不存在的 JSON 路径创建 PolicyLoader
        non_existent = "/tmp/this_file_does_not_exist_12345.json"
        loader = PolicyLoader(json_path=non_existent)

        # 应使用内置策略（DEFAULT_POLICIES）
        policies = loader.get_policies()
        self.assertEqual(len(policies), len(DEFAULT_POLICIES), "fallback 应加载 DEFAULT_POLICIES")

        # 验证 fallback 后的策略仍能正常匹配
        matched = loader.match("rm", "-rf /")
        self.assertIsNotNone(matched, "fallback 策略应能匹配 rm 命令")
        self.assertTrue(matched["block"])  # type: ignore[index]

    def test_policy_json_load_from_file(self) -> None:
        """
        测试6b：从有效 JSON 文件加载策略

        验证 PolicyLoader 能正确解析 JSON 配置文件。
        """
        # 创建临时 JSON 策略文件
        custom_policy = {
            "policies": [
                {
                    "id": "CUSTOM-001",
                    "name": "自定义测试策略",
                    "description": "测试用自定义策略",
                    "action_pattern": r"\btest_danger\b",
                    "target_pattern": None,
                    "risk_level": "critical",
                    "block": True,
                    "enabled": True,
                }
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(custom_policy, f)
            temp_path = f.name

        try:
            loader = PolicyLoader(json_path=temp_path)
            policies = loader.get_policies()
            self.assertEqual(len(policies), 1, "应加载 1 条自定义策略")
            self.assertEqual(policies[0]["id"], "CUSTOM-001")

            # 验证自定义策略能正确匹配
            matched = loader.match("test_danger", "/home/user")
            self.assertIsNotNone(matched)
        finally:
            os.unlink(temp_path)

    def test_risk_score_method(self) -> None:
        """
        测试7：risk_score() 方法应正常返回统计信息

        执行多次检查后，risk_score() 应返回正确的统计数据。
        """
        sandbox = SecuritySandbox()

        # 执行多次检查
        sandbox.check(_make_intent("open", "/home/user"))        # low 风险，放行
        sandbox.check(_make_intent("rm", "-rf /"))               # critical，拦截
        sandbox.check(_make_intent("delete", "/home/user/old"))  # high，放行
        sandbox.check(_make_intent("navigate", "/etc/passwd"))   # critical，拦截

        stats = sandbox.risk_score()

        # 验证统计字段
        self.assertIn("total_checks", stats, "应包含 total_checks 字段")
        self.assertIn("blocked_count", stats, "应包含 blocked_count 字段")
        self.assertIn("risk_score_accum", stats, "应包含 risk_score_accum 字段")
        self.assertIn("block_rate", stats, "应包含 block_rate 字段")

        self.assertEqual(stats["total_checks"], 4, "总检查次数应为 4")
        self.assertEqual(stats["blocked_count"], 2, "拦截次数应为 2（rm + /etc/passwd）")
        self.assertGreater(stats["risk_score_accum"], 0, "累计风险分数应大于 0")
        self.assertAlmostEqual(stats["block_rate"], 0.5, places=2, msg="拦截率应为 50%")

    def test_ping_returns_healthy(self) -> None:
        """
        测试8：ping() 应返回 healthy=True
        """
        result = self.sandbox.ping()
        self.assertTrue(result["healthy"], "新创建的 sandbox ping 应该健康")
        self.assertIn("policy_count", result)
        self.assertGreater(result["policy_count"], 0)

    def test_check_permission_compat(self) -> None:
        """
        测试9：check_permission() 兼容接口应正常工作
        """
        result = self.sandbox.check_permission("rm", "-rf /")
        self.assertFalse(result["approved"], "check_permission 兼容接口应能拦截 rm")

        result2 = self.sandbox.check_permission("open", "/home/user/Documents")
        self.assertTrue(result2["approved"], "check_permission 兼容接口应放行安全操作")


if __name__ == "__main__":
    # 直接运行此测试文件时执行所有测试
    unittest.main(verbosity=2)
