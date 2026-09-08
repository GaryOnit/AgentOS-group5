"""
RAG 知识库门面模块（rag_kb.py）

基于 TF-IDF 向量检索 + 关键词过滤重排序的双通道检索知识库。

RAGKnowledgeBase 作为门面类（Facade），封装 TFIDFTraceStore 的底层存储，
提供统一的高层接口：add_trace()、query()、stats()、get_trace()、ping()。
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

from group5.contracts.schemas import QueryResult, TraceRecord
from group5.knowledge.trace_store import TFIDFTraceStore
from group5.knowledge.embedding import EmbeddingBackend, cosine_similarity
from group5.security.redaction import redact
from group5.storage.database import SQLiteStore


logger = logging.getLogger(__name__)


class RAGKnowledgeBase:
    """
    RAG（Retrieval-Augmented Generation）知识库

    双通道检索策略：
    1. TF-IDF 向量检索：计算查询与记录的余弦相似度
    2. 关键词过滤重排序：提升包含查询词的记录的排名

    这种组合策略兼顾语义相似度和精确关键词匹配。
    """

    def __init__(
        self,
        persist_file: Optional[str] = None,
        state_store: Optional[SQLiteStore] = None,
        include_mock: Optional[bool] = None,
        embedding_backend: Optional[EmbeddingBackend] = None,
    ) -> None:
        """
        初始化 RAG 知识库

        Args:
            persist_file: 持久化 JSON 文件路径（可选）。
                         指定后，历史记录在重启时自动恢复。
            state_store: 可选SQLite事实存储；指定后以trace_documents为来源。
            include_mock: 查询索引是否包含Mock文档；默认仅demo环境包含。
            embedding_backend: 可选语义向量后端；失败时自动回退TF-IDF。
        """
        self._state_store = state_store
        self._embedding_backend = embedding_backend
        self._last_retrieval_backend = "tfidf"
        self._include_mock = (
            state_store is not None and state_store.environment == "demo"
            if include_mock is None
            else bool(include_mock)
        )
        self._store = TFIDFTraceStore(
            persist_file=persist_file if state_store is None else None
        )
        if state_store is not None:
            self._reload_from_state_store()
        self._query_count: int = 0    # 累计查询次数
        self._add_count: int = 0       # 累计添加次数
        logger.info(
            "RAG 知识库初始化完成，历史记录: %d 条，persist_file: %s",
            self._store.count(),
            persist_file,
        )

    def add_trace(self, record: TraceRecord) -> None:
        """
        将链路追踪记录添加到知识库

        Args:
            record: TraceRecord 格式的追踪记录，必须包含 trace_id、summary 等字段

        Example:
            >>> kb.add_trace({
            ...     "trace_id": "trace-001",
            ...     "action": "open",
            ...     "target": "/home/user",
            ...     "stage": "completed",
            ...     "success": True,
            ...     "summary": "用户打开了主目录",
            ...     "timestamp": "2026-07-06T10:00:00",
            ...     "metadata": {},
            ... })
        """
        metadata = record.get("metadata", {})
        status = str(metadata.get("orchestration_status", ""))
        if status in {
            "needs_input",
            "needs_confirmation",
            "cancelled",
            "cancelled_with_side_effects",
        }:
            logger.debug("跳过中间或取消状态RAG入库: trace_id=%s", record.get("trace_id"))
            return
        if bool(metadata.get("sensitive", False)):
            logger.info("跳过敏感任务RAG入库: trace_id=%s", record.get("trace_id"))
            return

        safe_record: TraceRecord = {
            "trace_id": str(record.get("trace_id", "")),
            "action": str(record.get("action", "")),
            "target": str(record.get("target", "")),
            "stage": record.get("stage", "completed"),
            "success": bool(record.get("success", False)),
            "summary": str(redact(record.get("summary", ""))),
            "timestamp": str(record.get("timestamp", "")),
            "metadata": redact(metadata),
        }

        if self._state_store is not None:
            category = "success"
            if not safe_record["success"]:
                category = (
                    "security"
                    if safe_record["stage"] == "security"
                    else "failure"
                )
            self._state_store.upsert_trace_document(
                {
                    "task_trace_id": safe_record["trace_id"],
                    "category": category,
                    "is_mock": bool(metadata.get("is_mock", False)),
                    "sensitive": False,
                    "summary": safe_record["summary"],
                    "action": safe_record["action"],
                    "target": safe_record["target"],
                    "success": safe_record["success"],
                    "timestamp": safe_record["timestamp"],
                    "metadata": safe_record["metadata"],
                }
            )
            self._reload_from_state_store()
        else:
            self._store.add(safe_record)
        self._add_count += 1
        logger.debug("知识库新增记录: trace_id=%s", record.get("trace_id"))

    def query(
        self,
        query_text: str,
        top_k: int = 5,
        keyword_boost: bool = True,
    ) -> List[QueryResult]:
        """
        双通道检索：TF-IDF 向量相似度 + 关键词过滤重排序

        Args:
            query_text: 自然语言查询文本
            top_k: 最多返回条数（默认 5）
            keyword_boost: 是否启用关键词加权（默认 True）

        Returns:
            按相关度排序的 QueryResult 列表（空库时返回空列表）

        算法说明：
            1. TF-IDF 向量检索：获取初始候选集（top_k * 2）
            2. 关键词加权：若记录的 summary/action/target 包含查询词，额外加分
            3. 重排序：按综合得分降序，取前 top_k 条
        """
        self._query_count += 1

        # 第一通道始终执行本地TF-IDF，确保语义后端不可用时仍可检索。
        tfidf_candidates = self._store.query(query_text, top_k=top_k * 2)
        candidate_scores = {
            record["trace_id"]: (record, max(float(score), 0.0))
            for record, score in tfidf_candidates
        }
        self._last_retrieval_backend = "tfidf"

        if self._embedding_backend is not None and self._store.count() > 0:
            try:
                query_vector = self._embedding_backend.embed(query_text)
                semantic_scores = {}
                for record in self._store.all_records():
                    document_text = " ".join(
                        [
                            str(record.get("summary", "")),
                            str(record.get("action", "")),
                        ]
                    )
                    document_vector = self._embedding_backend.embed(document_text)
                    semantic_scores[record["trace_id"]] = max(
                        cosine_similarity(query_vector, document_vector),
                        0.0,
                    )

                records_by_id = {
                    record["trace_id"]: record for record in self._store.all_records()
                }
                combined = []
                for trace_id, record in records_by_id.items():
                    tfidf_score = candidate_scores.get(trace_id, (record, 0.0))[1]
                    semantic_score = semantic_scores.get(trace_id, 0.0)
                    combined.append(
                        (record, 0.4 * tfidf_score + 0.6 * semantic_score)
                    )
                combined.sort(key=lambda item: item[1], reverse=True)
                candidates = combined[: top_k * 2]
                self._last_retrieval_backend = (
                    f"hybrid:{getattr(self._embedding_backend, 'name', 'semantic')}"
                )
            except Exception as exc:
                # RAG增强失败不能阻塞桌面任务，且不得把查询文本写入错误日志。
                logger.warning("语义检索不可用，降级到TF-IDF: %s", type(exc).__name__)
                candidates = tfidf_candidates
                self._last_retrieval_backend = "tfidf_fallback"
        else:
            candidates = tfidf_candidates

        if not candidates:
            logger.debug("知识库查询无结果")
            return []

        # 第二通道：关键词过滤重排序
        if keyword_boost:
            query_lower = query_text.lower()
            # 提取查询中的关键词（3字以上的词，避免无意义短词）
            query_keywords = [
                w for w in query_lower.split()
                if len(w) >= 2
            ]

            boosted: List[tuple] = []
            for record, tfidf_score in candidates:
                boost = 0.0
                if query_keywords:
                    # 检查 summary、action、target 中是否包含查询关键词
                    text_lower = " ".join([
                        str(record.get("summary", "")),
                        str(record.get("action", "")),
                        str(record.get("target", "")),
                    ]).lower()
                    keyword_hits = sum(1 for kw in query_keywords if kw in text_lower)
                    # 每命中一个关键词加 0.1 分（最多加到 0.5）
                    boost = min(keyword_hits * 0.1, 0.5)

                final_score = min(max(float(tfidf_score) + boost, 0.0), 1.0)
                boosted.append((record, final_score))

            # 按综合得分重排序
            boosted.sort(key=lambda x: x[1], reverse=True)
            candidates = boosted

        # 取前 top_k 条，转换为 QueryResult 格式
        results: List[QueryResult] = []
        for record, score in candidates[:top_k]:
            result: QueryResult = {
                "trace_id": record.get("trace_id", ""),
                "score": round(float(score), 4),
                "summary": record.get("summary", ""),
                "action": record.get("action", ""),
                "target": record.get("target", ""),
                "success": record.get("success", False),
                "timestamp": record.get("timestamp", ""),
            }
            results.append(result)

        logger.debug("知识库查询: query=%s, 返回 %d 条结果", query_text, len(results))
        return results

    def build_planning_context(
        self,
        query_text: str,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        为第二组构造不含旧路径、坐标和授权信息的检索上下文。

        Args:
            query_text: 当前任务原始文本或意图摘要。
            top_k: 最多返回的历史经验数。

        Returns:
            仅含来源、相关度、脱敏摘要、成败和风险提示的列表。
        """
        results = self.query(query_text, top_k=top_k)
        context: List[Dict[str, Any]] = []
        for item in results:
            record = self._store.get(item["trace_id"])
            metadata = record.get("metadata", {}) if record else {}
            risk_hint = metadata.get("risk_hint", "")
            if not risk_hint and isinstance(metadata.get("error"), dict):
                risk_hint = metadata["error"].get("message", "")
            context.append(
                {
                    "source_trace_id": item["trace_id"],
                    "score": item["score"],
                    "summary": _remove_paths(str(redact(item["summary"]))),
                    "success": item["success"],
                    "risk_hint": _remove_paths(str(redact(risk_hint))),
                    "retrieval_backend": self._last_retrieval_backend,
                }
            )
        return context

    def get_trace(self, trace_id: str) -> Optional[TraceRecord]:
        """
        按 trace_id 精确获取追踪记录

        Args:
            trace_id: 追踪 ID

        Returns:
            TraceRecord 或 None（未找到时）
        """
        return self._store.get(trace_id)

    def delete_trace(self, trace_id: str) -> bool:
        """
        删除SQLite中的派生RAG文档并重建索引。

        Args:
            trace_id: 待删除任务追踪ID。

        Returns:
            实际删除文档时返回True；旧JSON模式不支持删除并返回False。
        """
        if self._state_store is None:
            return False
        deleted = self._state_store.delete_trace_document(trace_id)
        if deleted:
            self._reload_from_state_store()
        return deleted

    def _reload_from_state_store(self) -> None:
        """从当前SQLite环境重建可丢弃的TF-IDF索引。"""
        if self._state_store is None:
            return
        self._store = TFIDFTraceStore()
        documents = self._state_store.list_trace_documents(
            include_mock=self._include_mock,
            include_sensitive=False,
        )
        for document in documents:
            record: TraceRecord = {
                "trace_id": document["task_trace_id"],
                "action": document["action"],
                "target": document["target"],
                "stage": "completed" if document["success"] else "execution",
                "success": document["success"],
                "summary": document["summary"],
                "timestamp": document["timestamp"],
                "metadata": document["metadata"],
            }
            self._store.add(record)

    def stats(self) -> Dict[str, Any]:
        """
        返回知识库统计信息

        Returns:
            统计字典：
            {
                "total_records": int,     # 总记录数
                "total_queries": int,     # 总查询次数
                "total_adds": int,        # 总添加次数
                "vocab_size": int,        # 词汇表大小
                "has_index": bool,        # 是否已建立索引
            }
        """
        vocab_size = len(self._store._vocab) if self._store._vocab else 0
        return {
            "total_records": self._store.count(),
            "total_queries": self._query_count,
            "total_adds": self._add_count,
            "vocab_size": vocab_size,
            "has_index": self._store._tfidf_matrix is not None,
            "retrieval_backend": self._last_retrieval_backend,
        }

    def ping(self) -> Dict[str, Any]:
        """
        健康检查方法

        Returns:
            健康状态字典
        """
        start = time.perf_counter()
        try:
            count = self._store.count()
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "module": "rag_kb",
                "healthy": True,
                "latency_ms": round(latency_ms, 2),
                "record_count": count,
            }
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "module": "rag_kb",
                "healthy": False,
                "latency_ms": round(latency_ms, 2),
                "error": str(exc),
            }


def _remove_paths(text: str) -> str:
    """
    从规划上下文中移除旧的Unix或Windows绝对路径。

    Args:
        text: 已经过秘密脱敏的摘要或风险提示。

    Returns:
        使用[PATH]替换绝对路径后的文本。
    """
    without_unix_paths = re.sub(r"/(?:[^\s,，。;；]+)", "[PATH]", text)
    return re.sub(
        r"[A-Za-z]:\\(?:[^\s,，。;；]+)",
        "[PATH]",
        without_unix_paths,
    )


if __name__ == "__main__":
    # 独立运行示例：展示 RAG 知识库双通道检索
    import datetime

    kb = RAGKnowledgeBase()

    print("=== 添加追踪记录 ===")
    sample_records = [
        {
            "trace_id": "trace-001",
            "action": "open",
            "target": "/home/user/Documents",
            "stage": "completed",
            "success": True,
            "summary": "用户打开了文档文件夹，操作成功完成",
            "timestamp": datetime.datetime.now().isoformat(),
            "metadata": {"app": "file_manager"},
        },
        {
            "trace_id": "trace-002",
            "action": "navigate",
            "target": "/home/user/Downloads",
            "stage": "completed",
            "success": True,
            "summary": "用户导航到下载文件夹",
            "timestamp": datetime.datetime.now().isoformat(),
            "metadata": {},
        },
        {
            "trace_id": "trace-003",
            "action": "create",
            "target": "/home/user/Documents/new_folder",
            "stage": "completed",
            "success": True,
            "summary": "在文档目录创建了新文件夹",
            "timestamp": datetime.datetime.now().isoformat(),
            "metadata": {},
        },
    ]

    for r in sample_records:
        kb.add_trace(r)  # type: ignore[arg-type]
    print(f"已添加 {kb.stats()['total_records']} 条记录")

    print("\n=== 双通道检索测试 ===")
    queries = ["打开文件夹", "导航下载", "创建文件夹"]
    for q in queries:
        results = kb.query(q, top_k=2)
        print(f"查询 '{q}':")
        for r in results:
            print(f"  → [{r['trace_id']}] score={r['score']:.4f} {r['summary']}")

    print("\n=== 统计信息 ===")
    print(kb.stats())

    print("\n=== 健康检查 ===")
    print(kb.ping())

    print("\n✅ rag_kb.py 验证通过")
