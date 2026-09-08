"""跨组适配器注册表的外部行为测试。"""

import unittest

from group5.adapters import AdapterRegistrationError, ModuleRegistry
from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import MockGroup2Planner


class _VersionedPlanner:
    """用于测试v1契约声明的最小规划适配器。"""

    contract_version = "1.0"
    mode = "test"
    capabilities = ("plan", "structured_errors")

    def plan(self, request: dict) -> dict:
        """
        返回最小计划。

        Args:
            request: 测试传入的规划请求。

        Returns:
            包含空步骤的测试计划。
        """
        return {"steps": [], "request": request}


class _UnsupportedPlanner(_VersionedPlanner):
    """显式声明未知契约版本的测试适配器。"""

    contract_version = "9.9"


class TestModuleRegistry(unittest.TestCase):
    """验证可信注册表对调用方公开的注册和查询行为。"""

    def test_legacy_mock_can_register_with_inferred_capability(self) -> None:
        """未声明版本的现有Mock应以legacy模式兼容注册。"""
        registry = ModuleRegistry()

        metadata = registry.register("group2", MockGroup2Planner())

        self.assertEqual(metadata["contract_version"], "legacy")
        self.assertIn("plan", metadata["capabilities"])
        self.assertIsNotNone(registry.get("group2"))

    def test_versioned_adapter_exposes_declared_metadata(self) -> None:
        """v1适配器应公开其模式、版本和能力。"""
        registry = ModuleRegistry()

        metadata = registry.register("group2", _VersionedPlanner())

        self.assertEqual(metadata["mode"], "test")
        self.assertEqual(metadata["contract_version"], "1.0")
        self.assertEqual(
            metadata["capabilities"], ["plan", "structured_errors"]
        )

    def test_unknown_group_name_is_rejected(self) -> None:
        """注册表不得接受未定义的模块标识。"""
        registry = ModuleRegistry()

        with self.assertRaisesRegex(AdapterRegistrationError, "不支持的模块标识"):
            registry.register("group9", _VersionedPlanner())

    def test_missing_required_method_is_rejected(self) -> None:
        """缺少规划方法的对象不得注册为第二组。"""
        registry = ModuleRegistry()

        with self.assertRaisesRegex(AdapterRegistrationError, "缺少必需公开能力"):
            registry.register("group2", object())

    def test_unsupported_contract_version_is_rejected(self) -> None:
        """显式未知版本必须在启动阶段拒绝。"""
        registry = ModuleRegistry()

        with self.assertRaisesRegex(AdapterRegistrationError, "不受支持的契约版本"):
            registry.register("group2", _UnsupportedPlanner())

    def test_coordinator_health_reports_adapter_metadata(self) -> None:
        """协调器健康检查应展示已注册适配器的模式和版本。"""
        coordinator = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
        )
        coordinator.register("group2", _VersionedPlanner())

        try:
            health = coordinator.health_check()
            detail = health["group2"]["detail"] or ""
            self.assertIn("mode=test", detail)
            self.assertIn("contract=1.0", detail)
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
