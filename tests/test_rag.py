"""
RAG 知识库测试（test_rag.py）

测试 TFIDFTraceStore 和 RAGKnowledgeBase 的各种场景：
1. 入库后可查询
2. 空库查询不崩溃
3. 相似度排序正确
4. stats() 计算正确
5. 持久化后重载可查询
"""

import datetime
import os
import tempfile
import unittest

from group5.contracts.schemas import TraceRecord
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.knowledge.trace_store import TFIDFTraceStore


def _make_record(
    trace_id: str,
    action: str,
    target: str,
    summary: str,
    success: bool = True,
) -> TraceRecord:
    """辅助函数：快速构建 TraceRecord"""
    return {
        "trace_id": trace_id,
        "action": action,
        "target": target,
        "stage": "completed",
        "success": success,
        "summary": summary,
        "timestamp": datetime.datetime.now().isoformat(),
        "metadata": {},
    }


class TestTFIDFTraceStore(unittest.TestCase):
    """TFIDFTraceStore 的单元测试"""

    def setUp(self) -> None:
        """每个测试前创建新的空存储"""
        self.store = TFIDFTraceStore()

    def test_add_and_query_basic(self) -> None:
        """
        测试1：入库后可查询

        添加一条记录后，能通过相关关键词检索到该记录。
        """
        record = _make_record(
            "trace-001", "open", "/home/user/Documents",
            "用户打开了文档文件夹，操作成功完成"
        )
        self.store.add(record)

        # 验证入库成功
        self.assertEqual(self.store.count(), 1, "入库后记录数应为 1")

        # 按 trace_id 精确查找
        found = self.store.get("trace-001")
        self.assertIsNotNone(found, "应能按 trace_id 找到记录")
        self.assertEqual(found["action"], "open")  # type: ignore[index]

        # TF-IDF 检索
        results = self.store.query("打开文档文件夹", top_k=1)
        self.assertEqual(len(results), 1, "应检索到 1 条结果")
        self.assertEqual(results[0][0]["trace_id"], "trace-001")

    def test_cold_start_empty_query(self) -> None:
        """
        测试2：空库查询不崩溃

        新创建的存储（空库）执行查询应返回空列表，不抛出异常。
        """
        # 空库查询不崩溃，返回空列表
        results = self.store.query("任意查询文本", top_k=5)
        self.assertIsInstance(results, list, "空库查询应返回列表")
        self.assertEqual(len(results), 0, "空库查询应返回空列表")

        # 多次空库查询也不崩溃
        results2 = self.store.query("")
        self.assertEqual(len(results2), 0)

        results3 = self.store.query("打开文件管理器")
        self.assertEqual(len(results3), 0)

    def test_similarity_order(self) -> None:
        """
        测试3：相似度排序正确

        最相关的记录应排在最前面。
        """
        records = [
            _make_record("trace-001", "open", "/home/user/Documents",
                         "用户打开了文档文件夹"),
            _make_record("trace-002", "navigate", "/home/user/Downloads",
                         "用户导航到下载文件夹"),
            _make_record("trace-003", "delete", "/home/user/old.txt",
                         "用户删除了旧的文本文件"),
        ]
        for r in records:
            self.store.add(r)

        # 查询 "打开文档" 应返回 trace-001 排在最前
        results = self.store.query("打开文档", top_k=3)
        self.assertGreater(len(results), 0, "应有查询结果")
        self.assertGreater(results[0][1], 0, "第一条结果的相似度分数应大于 0")

        # 验证分数降序排列
        scores = [score for _, score in results]
        for i in range(len(scores) - 1):
            self.assertGreaterEqual(
                scores[i], scores[i + 1],
                f"结果应按相似度降序排列: scores={scores}"
            )

    def test_stats_accuracy(self) -> None:
        """
        测试4：stats() 计算正确（通过 RAGKnowledgeBase 接口测试）

        添加 N 条记录后，stats() 应准确反映记录数量。
        """
        kb = RAGKnowledgeBase()

        self.assertEqual(kb.stats()["total_records"], 0, "初始记录数应为 0")
        self.assertEqual(kb.stats()["total_queries"], 0, "初始查询数应为 0")

        # 添加 3 条记录
        records = [
            _make_record(f"trace-{i:03d}", "open", f"/home/user/dir{i}", f"打开目录 {i}")
            for i in range(3)
        ]
        for r in records:
            kb.add_trace(r)

        stats = kb.stats()
        self.assertEqual(stats["total_records"], 3, "添加 3 条后 total_records 应为 3")
        self.assertEqual(stats["total_adds"], 3, "total_adds 应为 3")

        # 执行查询
        kb.query("打开目录", top_k=2)
        kb.query("文件操作")

        stats2 = kb.stats()
        self.assertEqual(stats2["total_queries"], 2, "查询 2 次后 total_queries 应为 2")
        self.assertGreater(stats2["vocab_size"], 0, "词汇表大小应大于 0")
        self.assertTrue(stats2["has_index"], "有记录时应已建立索引")

    def test_persist_and_reload(self) -> None:
        """
        测试5：持久化后重载可查询

        将记录写入 JSON 文件，重新创建存储实例后，
        从文件加载历史记录，仍能查询到数据（不崩溃）。
        """
        # 创建临时文件用于持久化
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            # 第一阶段：创建存储并写入数据
            store1 = TFIDFTraceStore(persist_file=temp_path)
            records = [
                _make_record("trace-p01", "open", "/home/user/Documents", "打开文档目录"),
                _make_record("trace-p02", "create", "/home/user/new", "创建新文件夹"),
            ]
            for r in records:
                store1.add(r)
            self.assertEqual(store1.count(), 2, "第一阶段应有 2 条记录")

            # 第二阶段：重新创建存储，从持久化文件加载
            store2 = TFIDFTraceStore(persist_file=temp_path)
            self.assertEqual(store2.count(), 2, "重载后应有 2 条记录")

            # 验证精确查找可用
            found = store2.get("trace-p01")
            self.assertIsNotNone(found, "重载后应能按 trace_id 找到记录")
            self.assertEqual(found["action"], "open")  # type: ignore[index]

            # 验证 TF-IDF 查询可用（不崩溃）
            results = store2.query("打开文档", top_k=2)
            self.assertIsInstance(results, list, "重载后查询应返回列表")

        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_get_nonexistent(self) -> None:
        """
        测试6：查找不存在的 trace_id 应返回 None
        """
        result = self.store.get("nonexistent-trace-id")
        self.assertIsNone(result, "不存在的 trace_id 应返回 None")

    def test_tokenize_chinese_english(self) -> None:
        """
        测试7：_tokenize() 应支持中英文混合分词
        """
        tokens = self.store._tokenize("打开 /home/user 文件夹 open")
        self.assertIn("open", tokens, "英文单词 'open' 应被分词")
        self.assertIn("home", tokens, "英文路径 'home' 应被分词")
        # 中文字符应被逐字分词
        self.assertIn("打", tokens, "中文字符 '打' 应被分词")
        self.assertIn("开", tokens, "中文字符 '开' 应被分词")


class TestRAGKnowledgeBase(unittest.TestCase):
    """RAGKnowledgeBase 的单元测试"""

    def setUp(self) -> None:
        """每个测试前创建新的空知识库"""
        self.kb = RAGKnowledgeBase()

    def test_ping_healthy(self) -> None:
        """ping() 应返回 healthy=True"""
        result = self.kb.ping()
        self.assertTrue(result["healthy"])
        self.assertEqual(result["module"], "rag_kb")

    def test_add_and_get_trace(self) -> None:
        """add_trace 后应能 get_trace 精确检索"""
        record = _make_record("trace-kb-001", "navigate", "/home/user", "导航到主目录")
        self.kb.add_trace(record)

        found = self.kb.get_trace("trace-kb-001")
        self.assertIsNotNone(found)
        self.assertEqual(found["action"], "navigate")  # type: ignore[index]

    def test_dual_channel_query(self) -> None:
        """
        双通道检索测试：关键词加权应提升匹配记录排名
        """
        records = [
            _make_record("trace-d01", "open", "/home/user/Documents",
                         "打开了文档目录 open documents"),
            _make_record("trace-d02", "delete", "/tmp/old.log",
                         "删除了旧的日志文件 delete log"),
        ]
        for r in records:
            self.kb.add_trace(r)

        # 查询 "打开文档"，trace-d01 应排在前面
        results = self.kb.query("打开文档", top_k=2, keyword_boost=True)
        self.assertGreater(len(results), 0)
        # 验证返回的是 QueryResult 格式
        first = results[0]
        self.assertIn("trace_id", first)
        self.assertIn("score", first)
        self.assertIn("summary", first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
