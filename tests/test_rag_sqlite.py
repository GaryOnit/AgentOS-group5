"""SQLite RAG文档筛选与环境隔离的外部行为测试。"""

import os
import tempfile
import unittest

from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.storage import SQLiteStore, resolve_database_path


def _record(trace_id: str, **metadata) -> dict:
    """
    构造任务级RAG测试记录。

    Args:
        trace_id: 稳定任务追踪ID。
        **metadata: 控制筛选行为的元数据。

    Returns:
        标准TraceRecord字典。
    """
    return {
        "trace_id": trace_id,
        "action": "open",
        "target": "/home/user/Documents",
        "stage": "completed",
        "success": True,
        "summary": "打开文档目录",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "metadata": metadata,
    }


class TestRAGSQLite(unittest.TestCase):
    """验证SQLite来源、筛选、脱敏、删除和重启恢复。"""

    def setUp(self) -> None:
        """在D盘测试临时目录中创建数据库。"""
        temp_root = os.environ.get("TEMP") or os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)

    def tearDown(self) -> None:
        """清理测试数据库。"""
        self.temp_dir.cleanup()

    def test_success_document_survives_restart(self) -> None:
        """合格任务写入后应能在新知识库实例中检索。"""
        path = resolve_database_path(self.temp_dir.name, "production")
        store = SQLiteStore(path, "production")
        first = RAGKnowledgeBase(state_store=store)
        first.add_trace(_record("rag-sqlite-001"))

        second = RAGKnowledgeBase(state_store=store)
        results = second.query("打开文档", top_k=1)
        self.assertEqual(results[0]["trace_id"], "rag-sqlite-001")
        store.close()

    def test_mock_document_is_excluded_from_production_index(self) -> None:
        """production默认不得召回is_mock文档。"""
        path = resolve_database_path(self.temp_dir.name, "production")
        store = SQLiteStore(path, "production")
        kb = RAGKnowledgeBase(state_store=store)
        kb.add_trace(_record("mock-trace", is_mock=True))

        self.assertEqual(kb.query("打开文档"), [])
        self.assertEqual(store.list_trace_documents(include_mock=True)[0]["is_mock"], True)
        store.close()

    def test_demo_environment_can_include_mock_document(self) -> None:
        """demo环境默认允许检索明确标记的Mock经验。"""
        path = resolve_database_path(self.temp_dir.name, "demo")
        store = SQLiteStore(path, "demo")
        kb = RAGKnowledgeBase(state_store=store)
        kb.add_trace(_record("demo-mock", is_mock=True))

        self.assertEqual(kb.query("打开文档")[0]["trace_id"], "demo-mock")
        store.close()

    def test_intermediate_cancelled_and_sensitive_records_are_skipped(self) -> None:
        """中间状态、取消和敏感记录不得写入RAG文档表。"""
        path = resolve_database_path(self.temp_dir.name, "production")
        store = SQLiteStore(path, "production")
        kb = RAGKnowledgeBase(state_store=store)
        kb.add_trace(_record("needs-input", orchestration_status="needs_input"))
        kb.add_trace(_record("cancelled", orchestration_status="cancelled"))
        kb.add_trace(_record("sensitive", sensitive=True))

        self.assertEqual(store.list_trace_documents(include_mock=True), [])
        store.close()

    def test_rag_delete_does_not_delete_audit(self) -> None:
        """用户清除RAG文档时审计事件应保持存在。"""
        path = resolve_database_path(self.temp_dir.name, "production")
        store = SQLiteStore(path, "production")
        kb = RAGKnowledgeBase(state_store=store)
        kb.add_trace(_record("deletable-trace"))
        store.append_audit_event(
            {
                "event_id": "audit-stays",
                "trace_id": "deletable-trace",
                "event_type": "complete",
                "level": "INFO",
                "message": "done",
                "payload": {},
                "timestamp": "2026-09-07T08:00:00+00:00",
            }
        )

        self.assertTrue(kb.delete_trace("deletable-trace"))
        self.assertEqual(kb.query("打开文档"), [])
        self.assertEqual(store.audit_count(), 1)
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
