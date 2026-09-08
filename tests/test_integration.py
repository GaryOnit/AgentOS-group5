"""
集成测试（test_integration.py）

端到端测试完整链路，验证各组件协同工作：
1. 全链路成功，验证审计记录和 RAG 入库
2. rm 命令被拦截，error.code == E1001
3. 多条入库后检索正确
4. 无安全模块时降级放行
"""

import time
import unittest

from group5.audit.audit_logger import AuditLogger
from group5.contracts.error_codes import ErrorCode
from group5.contracts.schemas import IntentJSON
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import (
    MockGroup2Planner,
    MockGroup3Executor,
    MockGroup4ToolRegistry,
)
from group5.security.security_sandbox import SecuritySandbox


def _make_intent(action: str, target: str, trace_id: str, raw_text: str = "") -> IntentJSON:
    """辅助函数：快速构建 IntentJSON"""
    return {
        "trace_id": trace_id,
        "action": action,
        "target": target,
        "params": {},
        "raw_text": raw_text or f"{action} {target}",
    }


class TestIntegration(unittest.TestCase):
    """集成测试：验证完整端到端链路"""

    def _create_coordinator(
        self,
        with_security: bool = True,
    ) -> tuple:
        """
        创建测试用协调器实例

        Args:
            with_security: 是否注册安全沙箱

        Returns:
            (SystemCoordinator, RAGKnowledgeBase, AuditLogger) 元组
        """
        rag_kb = RAGKnowledgeBase()
        audit = AuditLogger()
        security = SecuritySandbox() if with_security else None

        coord = SystemCoordinator(
            rag_kb=rag_kb,
            audit_logger=audit,
            security_sandbox=security,
        )
        coord.register("group2", MockGroup2Planner())
        coord.register("group3", MockGroup3Executor())
        coord.register("group4", MockGroup4ToolRegistry())

        return coord, rag_kb, audit

    def test_full_flow_with_mocks(self) -> None:
        """
        集成测试1：全链路成功，验证审计记录和 RAG 入库

        端到端执行一次安全意图，验证：
        1. 编排成功（success=True）
        2. 审计日志有记录
        3. RAG 知识库有新增记录
        """
        coord, rag_kb, audit = self._create_coordinator()
        trace_id = "integration-001"

        intent = _make_intent("open", "/home/user/Documents", trace_id, "打开文档文件夹")
        result = coord.orchestrate(intent)

        # 验证编排成功
        self.assertTrue(result["success"], f"全链路应成功，实际 error: {result.get('error')}")
        self.assertEqual(result["stage"], "completed")
        self.assertIsNone(result["error"])

        # 等待异步写入完成
        audit.flush(timeout=2.0)
        time.sleep(0.3)

        # 验证审计日志有记录
        events = audit.query_by_trace_id(trace_id)
        self.assertGreater(len(events), 0, "全链路完成后应有审计记录")

        # 验证有 orchestrate_start 和 orchestrate_complete 事件
        event_types = [e["event_type"] for e in events]
        self.assertIn("orchestrate_start", event_types, "应有 orchestrate_start 事件")
        self.assertIn("orchestrate_complete", event_types, "应有 orchestrate_complete 事件")

        # 验证 RAG 知识库有新增记录（等待异步入库）
        time.sleep(0.5)
        rag_stats = rag_kb.stats()
        self.assertGreaterEqual(rag_stats["total_records"], 1, "全链路成功后 RAG 应有记录")

        coord.shutdown(wait=False)

    def test_security_interception_e2e(self) -> None:
        """
        集成测试2：rm 命令被拦截，error.code == E1001

        端到端执行危险操作，验证：
        1. 安全拦截正常工作（success=False）
        2. 错误码为 E1001
        3. 不进入规划阶段（stage 应为 security）
        """
        coord, rag_kb, audit = self._create_coordinator()
        trace_id = "integration-e1001"

        intent = _make_intent("rm", "-rf /", trace_id, "删除所有文件")
        result = coord.orchestrate(intent)

        # 验证被拦截
        self.assertFalse(result["success"], "rm -rf / 应被拦截")
        self.assertIsNotNone(result["error"])
        self.assertEqual(
            result["error"]["code"], ErrorCode.E1001.value,  # type: ignore[index]
            f"安全拦截应返回 E1001，实际: {result['error']}"
        )
        self.assertEqual(result["stage"], "security", "应在 security 阶段停止")

        # 验证没有进入规划阶段（规划事件不应存在）
        audit.flush(timeout=2.0)
        time.sleep(0.2)
        events = audit.query_by_trace_id(trace_id)
        event_types = [e["event_type"] for e in events]
        self.assertNotIn("planning_complete", event_types, "不应进入规划阶段")

        coord.shutdown(wait=False)

    def test_rag_retrieval_after_multiple_traces(self) -> None:
        """
        集成测试3：多条入库后检索正确

        执行多次成功编排，验证 RAG 知识库能正确检索相关记录。
        """
        coord, rag_kb, audit = self._create_coordinator()

        # 执行多次成功编排
        intents = [
            _make_intent("open", "/home/user/Documents", f"rag-test-{i:03d}", f"打开文档目录 {i}")
            for i in range(3)
        ]
        intents += [
            _make_intent("navigate", "/home/user/Downloads", f"rag-nav-{i:03d}", f"导航到下载目录")
            for i in range(2)
        ]

        for intent in intents:
            result = coord.orchestrate(intent)
            self.assertTrue(result["success"], f"编排应成功: {intent}")

        # 等待异步 RAG 写入完成
        time.sleep(0.8)

        rag_stats = rag_kb.stats()
        # 至少应有编排次数相同数量的记录（成功 + 失败都会入库）
        self.assertGreaterEqual(
            rag_stats["total_records"], len(intents),
            f"RAG 应至少有 {len(intents)} 条记录，实际: {rag_stats['total_records']}"
        )

        # 验证 RAG 查询不崩溃
        results = rag_kb.query("打开文档目录", top_k=3)
        self.assertIsInstance(results, list, "RAG 查询应返回列表")

        coord.shutdown(wait=False)

    def test_degraded_mode_no_security(self) -> None:
        """
        集成测试4：无安全模块时降级放行

        当 SecuritySandbox 不可用（通过注入总是返回 approved=True 的 mock），
        验证协调器能降级运行（不崩溃，继续执行后续流程）。

        实现方式：创建一个总是返回 approved=True 的安全沙箱 mock。
        """

        class AlwaysPassSandbox:
            """总是放行的沙箱 mock（模拟安全模块禁用/降级场景）"""

            def check(self, intent_json: IntentJSON) -> dict:
                return {
                    "approved": True,
                    "risk_level": "low",
                    "reason": None,
                    "matched_policy": None,
                }

            def ping(self) -> dict:
                return {"module": "always_pass", "healthy": True, "latency_ms": 0.1}

            def risk_score(self) -> dict:
                return {"total_checks": 0, "blocked_count": 0, "risk_score_accum": 0, "block_rate": 0.0}

        # 使用降级安全沙箱
        rag_kb = RAGKnowledgeBase()
        audit = AuditLogger()
        coord = SystemCoordinator(
            rag_kb=rag_kb,
            audit_logger=audit,
            security_sandbox=AlwaysPassSandbox(),  # type: ignore[arg-type]
        )
        coord.register("group2", MockGroup2Planner())
        coord.register("group3", MockGroup3Executor())
        # 本用例只验证安全模块降级；MockGroup3会请求file_manager工具，
        # 因此仍需注册Group4，避免把工具缺失误当作安全降级失败。
        coord.register("group4", MockGroup4ToolRegistry())

        # 即使是危险操作，降级沙箱也应放行（继续到规划/执行）
        intent = _make_intent("rm", "-rf /", "degraded-001", "降级模式测试")
        result = coord.orchestrate(intent)

        # 降级模式下，安全检查放行，后续流程正常运行
        self.assertTrue(result["success"], f"降级模式应放行，实际: {result.get('error')}")
        self.assertEqual(result["stage"], "completed", "降级模式应完成全流程")

        coord.shutdown(wait=False)

    def test_coordinator_stats_after_mixed_results(self) -> None:
        """
        集成测试5：混合成功/失败后统计信息正确

        执行多次成功和失败的编排，验证 stats() 统计准确。
        """
        coord, _, _ = self._create_coordinator()

        # 2 次成功
        for i in range(2):
            coord.orchestrate(_make_intent("open", "/home/user", f"stats-ok-{i}"))

        # 1 次失败（安全拦截）
        coord.orchestrate(_make_intent("rm", "-rf /", "stats-fail-001"))

        stats = coord.stats()
        self.assertEqual(stats["total_orchestrations"], 3, "总编排次数应为 3")
        self.assertEqual(stats["success_count"], 2, "成功次数应为 2")
        self.assertEqual(stats["failure_count"], 1, "失败次数应为 1")

        coord.shutdown(wait=False)

    def test_multiple_coordinators_independent(self) -> None:
        """
        集成测试6：多个协调器实例相互独立

        每个协调器实例应有独立的 RAG 和审计日志，互不干扰。
        """
        coord1, rag1, audit1 = self._create_coordinator()
        coord2, rag2, audit2 = self._create_coordinator()

        # coord1 执行编排
        coord1.orchestrate(_make_intent("open", "/home/user", "coord1-trace-001"))

        # coord2 不执行任何操作
        time.sleep(0.3)
        audit1.flush(timeout=1.0)

        # coord2 的 RAG 应为空（与 coord1 独立）
        stats2 = rag2.stats()
        self.assertEqual(stats2["total_records"], 0, "coord2 的 RAG 应为空（与 coord1 独立）")

        coord1.shutdown(wait=False)
        coord2.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
