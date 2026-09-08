"""v1 公共契约的外部行为测试。"""

import unittest

from group5.contracts import (
    CONTRACT_VERSION_V1,
    ContractValidationError,
    ErrorCode,
    make_error,
    validate_orchestrate_result_v1,
    validate_task_envelope_v1,
)


def _valid_task() -> dict:
    """
    构造一份合法的 v1 任务信封。

    Returns:
        可提交给公开契约校验器的任务字典。
    """
    return {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": "task-001",
        "attempt_id": "attempt-001",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "应用控制",
            "action": "open",
            "target": "Files",
            "params": {},
            "raw_text": "打开文件管理器",
        },
    }


def _valid_result() -> dict:
    """
    构造一份合法的 v1 成功结果。

    Returns:
        可提交给公开结果校验器的结果字典。
    """
    return {
        "contract_version": CONTRACT_VERSION_V1,
        "success": True,
        "status": "completed",
        "task_trace_id": "task-001",
        "attempt_id": "attempt-001",
        "stage": "completed",
        "total_latency_ms": 12.5,
        "stages": [
            {
                "name": "intent",
                "status": "completed",
                "latency_ms": 3.0,
                "attempts": 1,
            }
        ],
        "result": {"message": "ok"},
        "error": None,
    }


class TestContractV1(unittest.TestCase):
    """验证 v1 任务和结果契约对调用方可观察的行为。"""

    def test_valid_task_envelope_is_accepted(self) -> None:
        """合法任务信封应原样通过公开校验器。"""
        task = _valid_task()
        self.assertIs(validate_task_envelope_v1(task), task)

    def test_unknown_task_version_is_rejected(self) -> None:
        """未知契约版本必须明确拒绝，不能静默按 v1 解析。"""
        task = _valid_task()
        task["contract_version"] = "2.0"

        with self.assertRaisesRegex(ContractValidationError, "不支持的契约版本"):
            validate_task_envelope_v1(task)

    def test_missing_task_field_is_rejected(self) -> None:
        """任务缺少追踪字段时应返回稳定校验错误。"""
        task = _valid_task()
        del task["attempt_id"]

        with self.assertRaisesRegex(ContractValidationError, "attempt_id"):
            validate_task_envelope_v1(task)

    def test_invalid_intent_params_are_rejected(self) -> None:
        """params 不是对象时不得进入后续安全和规划阶段。"""
        task = _valid_task()
        task["intent"]["params"] = []

        with self.assertRaisesRegex(ContractValidationError, "intent.params"):
            validate_task_envelope_v1(task)

    def test_valid_completed_result_is_accepted(self) -> None:
        """字段完整且状态一致的成功结果应通过校验。"""
        result = _valid_result()
        self.assertIs(validate_orchestrate_result_v1(result), result)

    def test_success_and_status_must_agree(self) -> None:
        """暂停或失败状态不能被错误报告为 success=True。"""
        result = _valid_result()
        result["status"] = "needs_input"

        with self.assertRaisesRegex(ContractValidationError, "不一致"):
            validate_orchestrate_result_v1(result)

    def test_failure_requires_structured_error(self) -> None:
        """失败结果必须提供 code、message 和 retryable。"""
        result = _valid_result()
        result.update({"success": False, "status": "failed", "error": None})

        with self.assertRaisesRegex(ContractValidationError, "result.error"):
            validate_orchestrate_result_v1(result)

    def test_intent_error_domain_does_not_conflict_with_security(self) -> None:
        """第一组错误映射使用 E01xx，不得复用 E1001 安全错误。"""
        intent_error = make_error(ErrorCode.E0101)
        security_error = make_error(ErrorCode.E1001)

        self.assertEqual(intent_error["code"], "E0101")
        self.assertEqual(security_error["code"], "E1001")
        self.assertNotEqual(intent_error["message"], security_error["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
