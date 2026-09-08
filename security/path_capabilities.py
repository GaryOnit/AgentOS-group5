"""Linux文件工具的真实路径解析与最小能力判定。"""

import os
import posixpath
import re
from typing import Dict, Iterable, List, TypedDict


class PathCapabilityError(ValueError):
    """路径无法安全授权。"""


class PathCapability(TypedDict):
    """经过规范化和真实路径检查的文件访问能力。"""

    input_path: str
    normalized_path: str
    real_path: str
    mode: str


def normalize_linux_path(path: str) -> str:
    """
    使用Linux语义规范化任务路径，不受当前开发主机系统影响。

    Args:
        path: 第一组或工具请求提供的Linux路径。

    Returns:
        使用正斜杠的规范路径。

    Raises:
        PathCapabilityError: 路径含NUL或不是字符串。
    """
    if not isinstance(path, str):
        raise PathCapabilityError("路径必须是字符串")
    if "\x00" in path:
        raise PathCapabilityError("路径不得包含NUL字符")
    if path.startswith("/") or path.startswith("./") or path.startswith("../"):
        return posixpath.normpath(path)
    return path


class PathCapabilityResolver:
    """
    根据安全根目录和受保护目录生成最小文件能力。

    Linux绝对路径始终使用POSIX边界判断；在真实Linux主机上还会解析符号
    链接。Windows开发环境中的本机绝对路径用于单元测试时使用os.path规则。
    """

    def __init__(
        self,
        allowed_roots: Iterable[str],
        protected_roots: Iterable[str],
    ) -> None:
        """
        初始化路径授权边界。

        Args:
            allowed_roots: 可按任务进一步缩小的安全根目录。
            protected_roots: 无条件禁止授权的系统目录。
        """
        self._allowed_roots = [self._canonical_root(path) for path in allowed_roots]
        self._protected_roots = [
            self._canonical_root(path) for path in protected_roots
        ]

    def resolve(self, path: str, mode: str) -> PathCapability:
        """
        验证路径并生成只读或读写能力。

        Args:
            path: 用户提供或规划生成的路径。
            mode: read或write。

        Returns:
            可用于工具授权清单的PathCapability。

        Raises:
            PathCapabilityError: 相对路径、保护路径或越界路径。
        """
        if mode not in {"read", "write"}:
            raise PathCapabilityError("能力模式必须是read或write")
        normalized = self._normalize_absolute(path)
        real_path = self._resolve_real_path(normalized)

        if any(_is_within(real_path, root) for root in self._protected_roots):
            raise PathCapabilityError("目标位于受保护目录")
        if not any(_is_within(real_path, root) for root in self._allowed_roots):
            raise PathCapabilityError("目标超出当前安全根目录")
        return {
            "input_path": path,
            "normalized_path": normalized,
            "real_path": real_path,
            "mode": mode,
        }

    def revalidate(self, capability: PathCapability) -> PathCapability:
        """
        执行前重新解析路径并检测目标替换。

        Args:
            capability: 安全检查阶段生成的原能力。

        Returns:
            与原真实目标一致的最新能力。

        Raises:
            PathCapabilityError: 真实目标在检查后发生变化。
        """
        current = self.resolve(capability["input_path"], capability["mode"])
        if current["real_path"] != capability["real_path"]:
            raise PathCapabilityError("路径真实目标在授权后发生变化")
        return current

    def resolve_arguments(
        self,
        arguments: Dict[str, object],
        path_modes: Dict[str, str],
    ) -> List[PathCapability]:
        """
        从工具参数中提取并授权声明的路径字段。

        Args:
            arguments: 标准工具请求参数。
            path_modes: 参数字段到read/write能力的映射。

        Returns:
            按path_modes顺序生成的最小能力列表。

        Raises:
            PathCapabilityError: 必需路径缺失、类型错误或越界。
        """
        capabilities: List[PathCapability] = []
        for field, mode in path_modes.items():
            value = arguments.get(field)
            if not isinstance(value, str) or not value:
                raise PathCapabilityError(f"工具参数缺少有效路径: {field}")
            capabilities.append(self.resolve(value, mode))
        return capabilities

    @staticmethod
    def _canonical_root(path: str) -> str:
        """规范化配置根目录并移除非根路径末尾分隔符。"""
        normalized = PathCapabilityResolver._normalize_absolute(path)
        return PathCapabilityResolver._resolve_real_path(normalized).rstrip("/\\") or normalized

    @staticmethod
    def _normalize_absolute(path: str) -> str:
        """按路径风格规范化绝对路径。"""
        if not isinstance(path, str) or not path:
            raise PathCapabilityError("路径必须是非空字符串")
        if "\x00" in path:
            raise PathCapabilityError("路径不得包含NUL字符")
        if path.startswith("/"):
            return posixpath.normpath(path)
        if re.match(r"^[A-Za-z]:[\\/]", path):
            return os.path.normpath(path)
        raise PathCapabilityError("文件能力只接受绝对路径")

    @staticmethod
    def _resolve_real_path(path: str) -> str:
        """解析本机可识别路径的符号链接和不存在目标的父目录。"""
        is_posix_task_path = path.startswith("/")
        if is_posix_task_path and os.name != "posix":
            # Windows开发机不能把Linux任务路径交给os.path.realpath，否则
            # 会错误转换为当前盘符路径；Linux验收环境会进入下方真实解析。
            return path

        if os.path.exists(path) or os.path.islink(path):
            return os.path.realpath(path)
        parent = os.path.dirname(path)
        basename = os.path.basename(path)
        return os.path.join(os.path.realpath(parent), basename)


def _is_within(path: str, root: str) -> bool:
    """
    使用目录边界判断路径是否位于根目录，防止/home/user2前缀欺骗。

    Args:
        path: 已规范化或真实路径。
        root: 已规范化安全根。

    Returns:
        路径等于根或位于根的子目录时返回True。
    """
    if path.startswith("/") and root.startswith("/"):
        return path == root or path.startswith(root.rstrip("/") + "/")
    normalized_path = os.path.normcase(os.path.normpath(path))
    normalized_root = os.path.normcase(os.path.normpath(root))
    try:
        return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
    except ValueError:
        return False


__all__ = [
    "PathCapability",
    "PathCapabilityError",
    "PathCapabilityResolver",
    "normalize_linux_path",
]
