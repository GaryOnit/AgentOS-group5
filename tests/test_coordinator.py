"""
协调器测试（test_coordinator.py）

测试 SystemCoordinator 的各种场景：
1. 全流程成功（happy path）
2. 安全拒绝返回 E1001
3. 未注册 group2 返回 E2001
4. 慢模块触发 E5002 超时
5. 无 trace_id 时自动生成 UUID
6. 失败时有审计记录
7. 注册模块后健康检查通过
"""

import time
import unittest
import uuid

from group5.audit.audit_logger import AuditLogger
from group5.contracts.error_codes import ErrorCode
from group5.contracts.schemas import IntentJSON
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import (
    MockGroup2Planner,
    MockGroup3Executor,
    MockGroup4ToolRegistry,
    SlowMockGroup2Planner,
)
from group5.security.security_sandbox import SecuritySandbox


def _make_intent(
    action: str,
    target: str,
    trace_id: str = "",
    raw_text: str = "",
) -> IntentJSON:
    """辅助函数：快速构建 IntentJSON"""
    return {
        "trace_id": trace_id,
        "action": action,
        "target": target,
        "params": {},
        "raw_text": raw_text or f"{action} {target}",
    }


def _make_full_coordinator() -> SystemCoordinator:
    """辅助函数：创建注册了所有模块的协调器"""
    coord = SystemCoordinator(
        rag_kb=RAGKnowledgeBase(),
        audit_logger=AuditLogger(),
        security_sandbox=SecuritySandbox(),
    )
    coord.register("group2", MockGroup2Planner())
    coord.register("group3", MockGroup3Executor())
    coord.register("group4", MockGroup4ToolRegistry())
    return coord


class TestSystemCoordinator(unittest.TestCase):
    """SystemCoordinator 的单元测试集"""

    def tearDown(self) -> None:
        """每个测试后短暂等待，确保异步任务完成"""
        time.sleep(0.1)

    def test_orchestrate_happy_path(self) -> None:
        """
        测试1：全流程成功（happy path）

        注册所有模块后，对安全意图执行编排，
        应返回 success=True，stage="completed"，error=None。
        """
        coord = _make_full_coordinator()

        intent = _make_intent("open", "/home/user/Documents", trace_id="happy-path-001")
        result = coord.orchestrate(intent)

        self.assertTrue(result["success"], "全流程应成功")
        self.assertEqual(result["stage"], "completed", "成功时 stage 应为 completed")
        self.assertIsNone(result["error"], "成功时 error 应为 None")
        self.assertEqual(result["trace_id"], "happy-path-001", "trace_id 应保持不变")
        self.assertIsNotNone(result["result"], "成功时 result 不应为 None")
        self.assertGreater(result["latency_ms"], 0, "latency_ms 应大于 0")

        coord.shutdown(wait=False)

    def test_security_block(self) -> None:
        """
        测试2：安全拒绝应返回 E1001

        rm -rf / 被安全沙箱拦截，应返回 success=False，
        error.code 应为 E1001，stage 应为 "security"。
        """
        coord = _make_full_coordinator()

        intent = _make_intent("rm", "-rf /", trace_id="security-block-001")
        result = coord.orchestrate(intent)

        self.assertFalse(result["success"], "危险操作应被拦截")
        self.assertIsNotNone(result["error"], "拦截时应有 error 字段")
        self.assertEqual(
            result["error"]["code"], ErrorCode.E1001.value,  # type: ignore[index]
            "安全拦截应返回 E1001"
        )
        self.assertEqual(result["stage"], "security", "安全拒绝时 stage 应为 security")

        coord.shutdown(wait=False)

    def test_planner_unavailable(self) -> None:
        """
        测试3：未注册 group2 应返回 E2001

        不注册 Group2 Planner，对安全意图编排，
        应在规划阶段失败，返回 E2001。
        """
        # 只注册 group3 和 group4，不注册 group2
        coord = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
        )
        coord.register("group3", MockGroup3Executor())
        coord.register("group4", MockGroup4ToolRegistry())

        intent = _make_intent("open", "/home/user", trace_id="no-planner-001")
        result = coord.orchestrate(intent)

        self.assertFalse(result["success"], "未注册 group2 应失败")
        self.assertEqual(
            result["error"]["code"], ErrorCode.E2001.value,  # type: ignore[index]
            "未注册 group2 应返回 E2001"
        )
        self.assertEqual(result["stage"], "planning", "应在规划阶段失败")

        coord.shutdown(wait=False)

    def test_timeout_handling(self) -> None:
        """
        测试4：慢模块触发 E5002

        为 Planner 注入短超时阈值并注册慢速实现，
        应返回 E5002 超时错误。
        """
        coord = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
            stage_timeouts={"planning": 0.05},
        )
        # 使用短测试阈值验证超时，不让单元测试真实等待默认15秒。
        coord.register("group2", SlowMockGroup2Planner(sleep_seconds=0.2))
        coord.register("group3", MockGroup3Executor())

        intent = _make_intent("open", "/home/user", trace_id="timeout-001")

        start = time.time()
        result = coord.orchestrate(intent)
        elapsed = time.time() - start

        self.assertFalse(result["success"], "超时应失败")
        self.assertEqual(
            result["error"]["code"], ErrorCode.E5002.value,  # type: ignore[index]
            "超时应返回 E5002"
        )
        # 注入50ms阈值后应快速失败，证明测试不依赖硬编码默认值。
        self.assertLess(elapsed, 1.0, f"超时控制应快速返回，实际: {elapsed:.1f}s")

        coord.shutdown(wait=False)

    def test_missing_trace_id_auto_gen(self) -> None:
        """
        测试5：无 trace_id 时自动生成 UUID

        传入 trace_id 为空的意图，协调器应自动生成 UUID。
        """
        coord = _make_full_coordinator()

        intent = _make_intent("open", "/home/user/Documents", trace_id="")
        result = coord.orchestrate(intent)

        # 自动生成的 trace_id 应是有效的非空字符串
        self.assertIsNotNone(result["trace_id"], "应自动生成 trace_id")
        self.assertNotEqual(result["trace_id"], "", "自动生成的 trace_id 不应为空")

        # 验证是合法的 UUID 格式
        try:
            uuid.UUID(result["trace_id"])
        except ValueError:
            self.fail(f"自动生成的 trace_id 应为 UUID 格式: {result['trace_id']}")

        coord.shutdown(wait=False)

    def test_audit_written_on_failure(self) -> None:
        """
        测试6：失败时应有审计记录

        安全拦截时，审计日志应记录相关事件
        （orchestrate_start + security_block 等）。
        """
        audit = AuditLogger()
        coord = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=audit,
        )
        coord.register("group2", MockGroup2Planner())
        coord.register("group3", MockGroup3Executor())

        trace_id = "audit-fail-001"
        intent = _make_intent("rm", "-rf /", trace_id=trace_id)
        result = coord.orchestrate(intent)

        # 等待异步审计写入完成
        audit.flush(timeout=2.0)
        time.sleep(0.2)

        self.assertFalse(result["success"])

        # 验证审计日志有相关记录
        events = audit.query_by_trace_id(trace_id)
        self.assertGreater(len(events), 0, "失败时应有审计记录")

        # 验证有 orchestrate_start 事件
        event_types = [e["event_type"] for e in events]
        self.assertIn("orchestrate_start", event_types, "应有 orchestrate_start 事件")
        # 验证有拦截相关事件
        has_block_event = any(
            "block" in et or "fail" in et or "error" in et
            for et in event_types
        )
        self.assertTrue(has_block_event, f"应有拦截相关审计事件，实际: {event_types}")

        coord.shutdown(wait=False)

    def test_health_check(self) -> None:
        """
        测试7：注册模块后健康检查通过

        注册所有模块后，health_check() 应返回各模块的健康状态，
        且内部组件（security、rag_kb、audit）均为 healthy=True。
        """
        coord = _make_full_coordinator()
        health = coord.health_check()

        # 内部组件应健康
        self.assertIn("security", health, "health_check 应包含 security 状态")
        self.assertIn("rag_kb", health, "health_check 应包含 rag_kb 状态")
        self.assertIn("audit", health, "health_check 应包含 audit 状态")

        self.assertTrue(health["security"]["healthy"], "security 应健康")
        self.assertTrue(health["rag_kb"]["healthy"], "rag_kb 应健康")
        self.assertTrue(health["audit"]["healthy"], "audit 应健康")

        # 已注册的外部模块也应在 health_check 结果中
        self.assertIn("group2", health, "health_check 应包含已注册的 group2")
        self.assertIn("group3", health, "health_check 应包含已注册的 group3")

        coord.shutdown(wait=False)

    def test_validate_input_missing_fields(self) -> None:
        """
        测试8：缺少必要字段时应返回 E5001

        没有 action 的意图应在输入校验阶段失败。
        """
        coord = _make_full_coordinator()

        # 缺少 action 字段
        bad_intent: IntentJSON = {
            "trace_id": "bad-input-001",
            "action": "",          # 空 action
            "target": "/home/user",
            "params": {},
            "raw_text": "",
        }
        result = coord.orchestrate(bad_intent)

        self.assertFalse(result["success"], "缺少 action 应失败")
        self.assertEqual(
            result["error"]["code"], ErrorCode.E5001.value,  # type: ignore[index]
            "输入校验失败应返回 E5001"
        )

        coord.shutdown(wait=False)

    def test_stats_tracking(self) -> None:
        """
        测试9：stats() 应正确跟踪统计信息
        """
        coord = _make_full_coordinator()

        # 执行成功操作
        coord.orchestrate(_make_intent("open", "/home/user", trace_id="stats-001"))
        # 执行失败操作（安全拦截）
        coord.orchestrate(_make_intent("rm", "-rf /", trace_id="stats-002"))

        stats = coord.stats()

        self.assertEqual(stats["total_orchestrations"], 2, "总编排次数应为 2")
        self.assertEqual(stats["success_count"], 1, "成功次数应为 1")
        self.assertEqual(stats["failure_count"], 1, "失败次数应为 1")
        self.assertAlmostEqual(stats["success_rate"], 0.5, places=2)

        coord.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
