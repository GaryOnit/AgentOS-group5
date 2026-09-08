"""可插拔语义检索、TF-IDF降级和规划上下文测试。"""

import unittest

from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase


def _record(trace_id: str, summary: str, action: str = "open") -> dict:
    """
    构造语义检索记录。

    Args:
        trace_id: 任务追踪ID。
        summary: 可检索摘要。
        action: 操作动作。

    Returns:
        标准TraceRecord字典。
    """
    return {
        "trace_id": trace_id,
        "action": action,
        "target": "/home/user/Downloads",
        "stage": "completed",
        "success": True,
        "summary": summary,
        "timestamp": "2026-09-07T08:00:00+00:00",
        "metadata": {},
    }


class _SemanticBackend:
    """按下载整理语义返回确定向量的测试后端。"""

    name = "fake-semantic"

    def embed(self, text: str):
        """将整理/归档/下载映射到同一向量方向。"""
        if any(word in text for word in ("整理", "归档", "下载")):
            return [1.0, 0.0]
        return [0.0, 1.0]


class _FailingBackend:
    """每次向量化都失败的测试后端。"""

    name = "failing"

    def embed(self, text: str):
        """模拟语义服务不可用。"""
        raise RuntimeError("embedding unavailable")


class _ContextCapturingGroup2:
    """记录v1规划请求中检索上下文的第二组。"""

    contract_version = "1.0"

    def __init__(self) -> None:
        """初始化最后请求。"""
        self.last_request = None

    def plan(self, request: dict) -> dict:
        """记录请求并返回组合式空计划。"""
        self.last_request = request
        return {"steps": [], "elements": {}}


class _CompletedGroup3:
    """直接完成计划的v1第三组。"""

    contract_version = "1.0"

    def execute(self, request: dict) -> dict:
        """返回标准完成结果。"""
        return {
            "status": "completed",
            "completed_step_ids": [],
            "failed_step_id": None,
            "checkpoint": None,
            "output": "done",
        }


class TestSemanticRAG(unittest.TestCase):
    """验证语义排序、降级和安全上下文传递。"""

    def test_semantic_backend_matches_synonym(self) -> None:
        """“归档”查询应召回“整理下载目录”记录。"""
        kb = RAGKnowledgeBase(embedding_backend=_SemanticBackend())
        kb.add_trace(_record("organize", "整理下载目录", action="organize"))
        kb.add_trace(_record("browser", "打开浏览器"))

        results = kb.query("归档文件", top_k=2)
        self.assertEqual(results[0]["trace_id"], "organize")
        self.assertEqual(kb.stats()["retrieval_backend"], "hybrid:fake-semantic")

    def test_embedding_failure_falls_back_to_tfidf(self) -> None:
        """语义后端异常不得阻塞本地检索。"""
        kb = RAGKnowledgeBase(embedding_backend=_FailingBackend())
        kb.add_trace(_record("documents", "打开文档目录"))

        results = kb.query("打开文档", top_k=1)
        self.assertEqual(results[0]["trace_id"], "documents")
        self.assertEqual(kb.stats()["retrieval_backend"], "tfidf_fallback")

    def test_planning_context_removes_old_paths_and_secrets(self) -> None:
        """规划上下文不得包含旧绝对路径或密钥。"""
        kb = RAGKnowledgeBase(embedding_backend=_SemanticBackend())
        kb.add_trace(
            _record(
                "safe-context",
                "整理 /home/user/Downloads 使用 sk-1234567890abcdef",
                action="organize",
            )
        )

        context = kb.build_planning_context("归档下载", top_k=1)
        serialized = str(context)
        self.assertNotIn("/home/user/Downloads", serialized)
        self.assertNotIn("sk-1234567890abcdef", serialized)
        self.assertIn("[PATH]", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_coordinator_passes_retrieval_context_to_v1_group2(self) -> None:
        """规划前RAG结果应只通过v1请求传给第二组。"""
        kb = RAGKnowledgeBase(embedding_backend=_SemanticBackend())
        kb.add_trace(_record("history", "整理下载目录", action="organize"))
        group2 = _ContextCapturingGroup2()
        coordinator = SystemCoordinator(
            rag_kb=kb,
            audit_logger=AuditLogger(),
        )
        coordinator.register("group2", group2)
        coordinator.register("group3", _CompletedGroup3())
        task = {
            "contract_version": "1.0",
            "task_trace_id": "current-task",
            "attempt_id": "current-attempt",
            "timestamp": "2026-09-07T09:00:00+00:00",
            "intent": {
                "category": "文件操作",
                "action": "organize",
                "target": "/home/user/Downloads",
                "params": {},
                "raw_text": "归档下载文件",
            },
        }
        try:
            result = coordinator.orchestrate_task(task)
            context = group2.last_request["retrieval_context"]

            self.assertTrue(result["success"])
            self.assertEqual(context[0]["source_trace_id"], "history")
            self.assertNotIn("target", context[0])
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
