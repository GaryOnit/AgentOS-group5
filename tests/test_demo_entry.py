"""演示入口模式标识、第一组接入和demo数据库隔离测试。"""

import os
import tempfile
import unittest

from group5.storage import SQLiteStore
from main import create_demo_coordinator


class TestDemoEntry(unittest.TestCase):
    """验证演示工厂不会使用生产数据库或隐藏Mock模式。"""

    def setUp(self) -> None:
        """在D盘临时目录准备独立demo数据库。"""
        temp_root = os.environ.get("TEMP") or os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.database_path = os.path.join(self.temp_dir.name, "demo.sqlite3")

    def tearDown(self) -> None:
        """清理demo测试数据库。"""
        self.temp_dir.cleanup()

    def test_offline_mock_text_entry_uses_demo_database(self) -> None:
        """无外组依赖时自然语言入口应可运行并明确记录Mock轨迹。"""
        coordinator, _, audit = create_demo_coordinator(
            mode="mock",
            database_path=self.database_path,
        )
        try:
            result = coordinator.orchestrate_text("打开文件管理器")
            self.assertTrue(result["success"])
            health = coordinator.health_check()
            self.assertIn("mode=mock", health["group1"]["detail"])
            self.assertIn("contract=1.0", health["group1"]["detail"])
            audit.flush()
        finally:
            coordinator.shutdown(wait=True)

        store = SQLiteStore(self.database_path, "demo")
        try:
            documents = store.list_trace_documents(include_mock=True)
            self.assertEqual(len(documents), 1)
            self.assertTrue(documents[0]["is_mock"])
            self.assertGreater(store.audit_count(), 0)
        finally:
            store.close()

    def test_real_group1_facade_can_run_in_forced_mock_mode(self) -> None:
        """父目录第一组HostAgent应通过公开接口接入且不访问网络。"""
        expected_source = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "ai-shell-hostagent-update-liujiyuan")
        )
        if not os.path.isdir(expected_source):
            self.skipTest("当前工作区未提供第一组源码")

        coordinator, _, _ = create_demo_coordinator(
            mode="group1-mock",
            database_path=self.database_path,
        )
        try:
            result = coordinator.orchestrate_text("打开文件管理器")
            health = coordinator.health_check()

            self.assertTrue(result["success"])
            self.assertIn("mode=mock", health["group1"]["detail"])
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
