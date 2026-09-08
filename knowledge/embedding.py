"""可插拔语义向量后端协议与纯Python余弦相似度。"""

import math
from typing import Protocol, Sequence


class EmbeddingBackend(Protocol):
    """语义向量提供方必须满足的最小公开协议。"""

    name: str

    def embed(self, text: str) -> Sequence[float]:
        """
        将文本转换为固定维度数值向量。

        Args:
            text: 已脱敏的查询或任务摘要。

        Returns:
            非空、固定维度的浮点序列。
        """
        ...


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """
    计算两个语义向量的余弦相似度。

    Args:
        left: 查询向量。
        right: 文档向量。

    Returns:
        -1到1之间的相似度；空向量、维度不等或零范数返回0。
    """
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm < 1e-12 or right_norm < 1e-12:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = ["EmbeddingBackend", "cosine_similarity"]
