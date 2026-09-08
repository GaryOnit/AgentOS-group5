"""审计递归脱敏与SQLite持久化的外部行为测试。"""

import json
import os
import tempfile
import sqlite3
import unittest

from group5.audit.audit_logger import AuditLogger
from group5.storage import SQLiteStore, resolve_database_path


class TestAuditRedaction(unittest.TestCase):
    """验证秘密不会出现在内存、JSONL或SQLite查询结果中。"""

    def setUp(self) -> None:
        """在D盘测试临时目录中准备隔离输出。"""
        temp_root = os.environ.get("TEMP") or os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)

    def tearDown(self) -> None:
        """清理本用例创建的数据库和日志文件。"""
        self.temp_dir.cleanup()

    def test_nested_payload_is_redacted_before_memory_query(self) -> None:
        """嵌套敏感键和值模式在进入内存查询前应被替换。"""
        audit = AuditLogger()
        secret = "sk-1234567890abcdef"
        try:
            audit.log(
                "trace-redact",
                "secret_test",
                f"Authorization: Bearer abcdefghijklmnop and {secret}",
                payload={
                    "api_key": secret,
                    "nested": [{"password": "p@ss"}, {"safe": "visible"}],
                },
            )
            audit.flush()
            events = audit.query_by_trace_id("trace-redact")
            serialized = json.dumps(events, ensure_ascii=False)

            self.assertNotIn(secret, serialized)
            self.assertNotIn("abcdefghijklmnop", serialized)
            self.assertNotIn("p@ss", serialized)
            self.assertIn("[REDACTED]", serialized)
            self.assertIn("visible", serialized)
        finally:
            audit.shutdown(wait=True)

    def test_jsonl_output_contains_only_redacted_values(self) -> None:
        """兼容JSONL文件不得写入原始token。"""
        log_path = os.path.join(self.temp_dir.name, "audit.jsonl")
        audit = AuditLogger(log_file=log_path)
        try:
            audit.log(
                "trace-jsonl",
                "secret_test",
                "write audit",
                payload={"token": "raw-token-value"},
            )
            audit.flush()
        finally:
            audit.shutdown(wait=True)

        with open(log_path, "r", encoding="utf-8") as file:
            content = file.read()
        self.assertNotIn("raw-token-value", content)
        self.assertIn("[REDACTED]", content)

    def test_sqlite_audit_survives_logger_restart(self) -> None:
        """SQLite审计应在新Logger实例中按trace_id恢复查询。"""
        database_path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(database_path, "test")
        audit = AuditLogger(state_store=store)
        try:
            audit.log(
                "trace-sqlite",
                "persist_test",
                "stored",
                payload={"attempt_id": "attempt-001", "secret": "hidden"},
            )
            audit.flush()
            self.assertEqual(audit.count(), 1)
        finally:
            audit.shutdown(wait=True)
            store.close()

        reopened_store = SQLiteStore(database_path, "test")
        reopened_audit = AuditLogger(state_store=reopened_store)
        try:
            events = reopened_audit.query_by_trace_id("trace-sqlite")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["secret"], "[REDACTED]")
        finally:
            reopened_audit.shutdown(wait=True)
            reopened_store.close()

    def test_hash_chain_detects_modified_event(self) -> None:
        """直接修改SQLite审计内容后公开校验必须失败。"""
        database_path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(database_path, "test")
        audit = AuditLogger(state_store=store)
        try:
            audit.log("trace-chain", "one", "first")
            audit.log("trace-chain", "two", "second")
            audit.flush()
            self.assertTrue(store.verify_audit_chain()["valid"])

            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "UPDATE audit_events SET message = ? WHERE event_type = ?",
                    ("tampered", "one"),
                )
                connection.commit()
            finally:
                connection.close()

            verification = store.verify_audit_chain()
            self.assertFalse(verification["valid"])
            self.assertIsNotNone(verification["broken_event_id"])
        finally:
            audit.shutdown(wait=True)
            store.close()

    def test_hash_chain_detects_deleted_event(self) -> None:
        """删除链首事件后下一事件的previous_hash应暴露断链。"""
        database_path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(database_path, "test")
        audit = AuditLogger(state_store=store)
        try:
            audit.log("trace-delete", "one", "first")
            audit.log("trace-delete", "two", "second")
            audit.flush()
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "DELETE FROM audit_events WHERE event_type = ?",
                    ("one",),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertFalse(store.verify_audit_chain()["valid"])
        finally:
            audit.shutdown(wait=True)
            store.close()

    def test_export_contains_hashes_and_verification(self) -> None:
        """JSONL导出应包含链字段并报告导出前校验结果。"""
        database_path = resolve_database_path(self.temp_dir.name, "test")
        export_path = os.path.join(self.temp_dir.name, "audit-export.jsonl")
        store = SQLiteStore(database_path, "test")
        audit = AuditLogger(state_store=store)
        try:
            audit.log("trace-export", "export", "safe")
            audit.flush()
            result = store.export_audit_jsonl(export_path)

            self.assertEqual(result["exported_events"], 1)
            self.assertTrue(result["verification"]["valid"])
            with open(export_path, "r", encoding="utf-8") as file:
                exported = json.loads(file.readline())
            self.assertTrue(exported["chain_id"])
            self.assertTrue(exported["event_hash"])
        finally:
            audit.shutdown(wait=True)
            store.close()

    def test_retention_prunes_only_closed_chain(self) -> None:
        """保留清理只能删除完整关闭链，活动链必须保留。"""
        database_path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(database_path, "test")
        try:
            store.append_audit_event(
                {
                    "event_id": "old-event",
                    "trace_id": "old-trace",
                    "event_type": "old",
                    "level": "INFO",
                    "message": "old",
                    "payload": {},
                    "timestamp": "2020-01-01T00:00:00+00:00",
                }
            )
            store.rotate_audit_chain()
            store.append_audit_event(
                {
                    "event_id": "active-event",
                    "trace_id": "active-trace",
                    "event_type": "active",
                    "level": "INFO",
                    "message": "active",
                    "payload": {},
                    "timestamp": "2026-09-07T00:00:00+00:00",
                }
            )

            deleted = store.prune_closed_audit_chains(
                "2025-01-01T00:00:00+00:00"
            )
            self.assertEqual(deleted, 1)
            self.assertEqual(store.audit_count(), 1)
            self.assertTrue(store.verify_audit_chain()["valid"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
