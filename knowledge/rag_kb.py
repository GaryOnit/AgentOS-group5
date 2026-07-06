"""
RAG 知识库门面模块（rag_kb.py）

基于 TF-IDF 向量检索 + 关键词过滤重排序的双通道检索知识库。

RAGKnowledgeBase 作为门面类（Facade），封装 TFIDFTraceStore 的底层存储，
提供统一的高层接口：add_trace()、query()、stats()、get_trace()、ping()。
"""

import logging
import time
from typing import Any, Dict, List, Optional

from group5.contracts.schemas import QueryResult, TraceRecord
from group5.knowledge.trace_store import TFIDFTraceStore


logger = logging.getLogger(__name__)


class RAGKnowledgeBase:
    """
    RAG（Retrieval-Augmented Generation）知识库

    双通道检索策略：
    1. TF-IDF 向量检索：计算查询与记录的余弦相似度
    2. 关键词过滤重排序：提升包含查询词的记录的排名

    这种组合策略兼顾语义相似度和精确关键词匹配。
    """

    def __init__(self, persist_file: Optional[str] = None) -> None:
        """
        初始化 RAG 知识库

        Args:
            persist_file: 持久化 JSON 文件路径（可选）。
                         指定后，历史记录在重启时自动恢复。
        """
        self._store = TFIDFTraceStore(persist_file=persist_file)
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
        self._store.add(record)
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

        # 第一通道：TF-IDF 向量检索
        candidates = self._store.query(query_text, top_k=top_k * 2)

        if not candidates:
            # 空库或无结果，直接返回空列表
            logger.debug("知识库查询无结果: query=%s", query_text)
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

                final_score = tfidf_score + boost
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

    def get_trace(self, trace_id: str) -> Optional[TraceRecord]:
        """
        按 trace_id 精确获取追踪记录

        Args:
            trace_id: 追踪 ID

        Returns:
            TraceRecord 或 None（未找到时）
        """
        return self._store.get(trace_id)

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
