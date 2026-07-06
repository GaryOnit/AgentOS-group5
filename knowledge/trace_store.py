"""
TF-IDF 链路追踪存储模块（trace_store.py）

实现基于 TF-IDF（词频-逆文档频率）+ 余弦相似度的轻量级向量检索。
优先使用 numpy；若环境缺少 numpy，自动降级为纯 Python 计算，保证可运行性。

主要类：
    TFIDFTraceStore: 链路追踪记录的存储和 TF-IDF 向量检索引擎
"""

import json
import logging
import math
import os
import re
from typing import Dict, List, Optional, Tuple

from group5.contracts.schemas import TraceRecord


logger = logging.getLogger(__name__)

try:
    import numpy as _np  # type: ignore
except Exception:  # pragma: no cover
    _np = None


class TFIDFTraceStore:
    """
    基于 TF-IDF + 余弦相似度的链路追踪存储引擎

    特性：
    - 支持中英文混合分词（re.findall 同时匹配中文字符和英文单词）
    - 全量重建 TF-IDF 矩阵（每次 add 后重建，适合小规模数据）
    - 支持 JSON 持久化（persist_file 参数）
    - 冷启动（空库）查询不崩溃，返回空列表
    - numpy 不可用时自动降级为纯 Python 模式
    """

    def __init__(self, persist_file: Optional[str] = None) -> None:
        """
        初始化 TF-IDF 存储引擎

        Args:
            persist_file: 持久化文件路径（JSON 格式）。
                         若指定且文件存在，则自动加载历史数据；
                         若为 None，则纯内存模式。
        """
        self._persist_file = persist_file
        self._records: List[TraceRecord] = []
        self._vocab: List[str] = []

        # numpy 模式: list[list[float]] / numpy.ndarray 二选一
        self._tfidf_matrix: Optional[object] = None

        if _np is None:
            logger.warning("未检测到 numpy，TF-IDF 检索使用纯 Python 降级模式")

        # 冷启动：若持久化文件存在，则加载历史数据
        if persist_file and os.path.exists(persist_file):
            self._load_from_file()
            logger.info("从持久化文件加载 %d 条记录: %s", len(self._records), persist_file)

    def _tokenize(self, text: str) -> List[str]:
        """中英文混合分词。"""
        if not text:
            return []
        tokens = re.findall(r"[\u4e00-\u9fff]|[a-zA-Z]+", text)
        return [t.lower() for t in tokens]

    def _build_vocab(self) -> List[str]:
        """从所有记录中构建词汇表。"""
        vocab_set: set = set()
        for record in self._records:
            for field in [record.get("summary", ""), record.get("action", ""), record.get("target", "")]:
                tokens = self._tokenize(str(field))
                vocab_set.update(tokens)
        return sorted(vocab_set)

    def _compute_tf(self, tokens: List[str]) -> Dict[str, float]:
        """计算词频 TF。"""
        if not tokens:
            return {}
        total = len(tokens)
        tf: Dict[str, float] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        return {t: count / total for t, count in tf.items()}

    def _compute_idf(self) -> Dict[str, float]:
        """计算逆文档频率 IDF。"""
        n = len(self._records)
        if n == 0:
            return {}

        idf: Dict[str, float] = {}
        for token in self._vocab:
            df = sum(
                1 for record in self._records
                if token in self._tokenize(
                    f"{record.get('summary', '')} {record.get('action', '')} {record.get('target', '')}"
                )
            )
            idf[token] = math.log(n / (1 + df))
        return idf

    def _rebuild_tfidf(self) -> None:
        """全量重建 TF-IDF 矩阵。"""
        if not self._records:
            self._vocab = []
            self._tfidf_matrix = None
            return

        self._vocab = self._build_vocab()
        if not self._vocab:
            self._tfidf_matrix = None
            return

        idf = self._compute_idf()
        n = len(self._records)
        v = len(self._vocab)
        vocab_index = {token: idx for idx, token in enumerate(self._vocab)}

        if _np is not None:
            matrix = _np.zeros((n, v), dtype=_np.float32)
        else:
            matrix = [[0.0 for _ in range(v)] for _ in range(n)]

        for row_idx, record in enumerate(self._records):
            text = f"{record.get('summary', '')} {record.get('action', '')} {record.get('target', '')}"
            tokens = self._tokenize(text)
            tf = self._compute_tf(tokens)
            for token, tf_val in tf.items():
                if token in vocab_index:
                    col_idx = vocab_index[token]
                    value = tf_val * idf.get(token, 0.0)
                    if _np is not None:
                        matrix[row_idx, col_idx] = value
                    else:
                        matrix[row_idx][col_idx] = value

        self._tfidf_matrix = matrix
        logger.debug("TF-IDF 矩阵重建完成: shape=(%d, %d)", n, v)

    def _text_to_tfidf_vec(self, query: str) -> Optional[object]:
        """将查询文本转换为 TF-IDF 向量。"""
        if not self._vocab:
            return None

        vocab_index = {token: idx for idx, token in enumerate(self._vocab)}
        tokens = self._tokenize(query)
        if not tokens:
            return None

        tf = self._compute_tf(tokens)
        if _np is not None:
            vec = _np.zeros(len(self._vocab), dtype=_np.float32)
        else:
            vec = [0.0 for _ in range(len(self._vocab))]

        for token, tf_val in tf.items():
            if token in vocab_index:
                idx = vocab_index[token]
                if _np is not None:
                    vec[idx] = tf_val
                else:
                    vec[idx] = tf_val

        return vec

    def _cosine_similarity(self, vec_a: object, vec_b: object) -> float:
        """计算余弦相似度。"""
        if _np is not None:
            norm_a = _np.linalg.norm(vec_a)
            norm_b = _np.linalg.norm(vec_b)
            if norm_a < 1e-9 or norm_b < 1e-9:
                return 0.0
            return float(_np.dot(vec_a, vec_b) / (norm_a * norm_b))

        # 纯 Python 降级实现
        a = vec_a  # type: ignore[assignment]
        b = vec_b  # type: ignore[assignment]
        dot = 0.0
        sum_a = 0.0
        sum_b = 0.0
        for x, y in zip(a, b):
            dot += x * y
            sum_a += x * x
            sum_b += y * y
        if sum_a < 1e-18 or sum_b < 1e-18:
            return 0.0
        return float(dot / (math.sqrt(sum_a) * math.sqrt(sum_b)))

    def add(self, record: TraceRecord) -> None:
        """添加一条追踪记录并重建 TF-IDF 矩阵。"""
        self._records.append(record)
        self._rebuild_tfidf()

        if self._persist_file:
            self._save_to_file()

        logger.debug("添加追踪记录: trace_id=%s", record.get("trace_id", "unknown"))

    def query(self, query_text: str, top_k: int = 5) -> List[Tuple[TraceRecord, float]]:
        """基于 TF-IDF 余弦相似度查询最相关记录。"""
        if not self._records or self._tfidf_matrix is None:
            return []

        query_vec = self._text_to_tfidf_vec(query_text)
        if query_vec is None:
            return []

        scores: List[Tuple[int, float]] = []
        for i in range(len(self._records)):
            doc_vec = self._tfidf_matrix[i]  # type: ignore[index]
            score = self._cosine_similarity(query_vec, doc_vec)
            scores.append((i, score))

        scores.sort(key=lambda x: x[1], reverse=True)

        results: List[Tuple[TraceRecord, float]] = []
        for idx, score in scores[:top_k]:
            results.append((self._records[idx], score))

        return results

    def get(self, trace_id: str) -> Optional[TraceRecord]:
        """按 trace_id 精确查找记录。"""
        for record in self._records:
            if record.get("trace_id") == trace_id:
                return record
        return None

    def count(self) -> int:
        """返回存储记录总数。"""
        return len(self._records)

    def all_records(self) -> List[TraceRecord]:
        """返回所有追踪记录副本。"""
        return list(self._records)

    def _save_to_file(self) -> None:
        """将所有记录序列化并写入 JSON 持久化文件。"""
        try:
            data = {"records": list(self._records)}
            with open(self._persist_file, "w", encoding="utf-8") as f:  # type: ignore[arg-type]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("持久化写入失败: %s", exc)

    def _load_from_file(self) -> None:
        """从 JSON 持久化文件加载历史记录。"""
        try:
            with open(self._persist_file, "r", encoding="utf-8") as f:  # type: ignore[arg-type]
                data = json.load(f)
            self._records = data.get("records", [])
            self._rebuild_tfidf()
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("持久化文件加载失败，以空库启动: %s", exc)
            self._records = []
