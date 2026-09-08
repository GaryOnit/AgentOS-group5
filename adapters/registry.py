"""跨组模块的可信注册表。"""

from typing import Any, Dict, Iterable, List, Optional, Tuple, TypedDict

from group5.contracts.schemas import CONTRACT_VERSION_V1


class AdapterMetadata(TypedDict):
    """注册后可供健康检查和联调展示的适配器元数据。"""

    module: str
    mode: str
    contract_version: str
    capabilities: List[str]
    implementation: str


class AdapterRegistrationError(ValueError):
    """适配器名称、能力或契约版本不符合注册要求。"""


# 每组至少满足一组候选方法。第一组同时兼容当前 Mock 的 parse 和真实代码的
# understand_intent；后续 Group1 专用适配器会统一暴露标准调用方式。
_REQUIRED_METHODS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "group1": (("understand_intent",), ("parse",)),
    "group2": (("plan",),),
    "group3": (("execute",),),
    "group4": (("call_tool",),),
}

_SUPPORTED_VERSIONS = {"legacy", CONTRACT_VERSION_V1}


class ModuleRegistry:
    """
    第五组进程内可信模块注册表。

    注册只接受预期组名和满足最小公开能力的实例。未声明契约版本的现有模块
    被标记为 legacy，以便渐进迁移；显式声明未知版本的模块会被立即拒绝。
    """

    def __init__(self) -> None:
        """初始化空注册表和元数据表。"""
        self._modules: Dict[str, Any] = {}
        self._metadata: Dict[str, AdapterMetadata] = {}

    def register(self, group_name: str, module: Any) -> AdapterMetadata:
        """
        校验并注册一个外组适配器。

        Args:
            group_name: 固定组标识 group1、group2、group3 或 group4。
            module: 满足对应最小公开方法的适配器实例。

        Returns:
            注册后的只读元数据副本。

        Raises:
            AdapterRegistrationError: 组名、能力或版本不受支持。
        """
        normalized_name = str(group_name).strip().lower()
        if normalized_name not in _REQUIRED_METHODS:
            raise AdapterRegistrationError(f"不支持的模块标识: {group_name}")
        if module is None:
            raise AdapterRegistrationError(f"{normalized_name} 适配器不能为空")

        candidates = _REQUIRED_METHODS[normalized_name]
        if not any(self._has_methods(module, names) for names in candidates):
            expected = " 或 ".join("+".join(names) for names in candidates)
            raise AdapterRegistrationError(
                f"{normalized_name} 缺少必需公开能力: {expected}"
            )

        contract_version = self._read_contract_version(module)
        if contract_version not in _SUPPORTED_VERSIONS:
            raise AdapterRegistrationError(
                f"{normalized_name} 使用不受支持的契约版本: {contract_version}"
            )

        metadata: AdapterMetadata = {
            "module": normalized_name,
            "mode": str(getattr(module, "mode", "legacy")),
            "contract_version": contract_version,
            "capabilities": self._read_capabilities(module),
            "implementation": type(module).__name__,
        }
        self._modules[normalized_name] = module
        self._metadata[normalized_name] = metadata
        return self.metadata(normalized_name)  # type: ignore[return-value]

    def get(self, group_name: str, default: Any = None) -> Any:
        """
        获取已注册模块。

        Args:
            group_name: 模块标识。
            default: 未注册时返回的默认值。

        Returns:
            模块实例或默认值。
        """
        return self._modules.get(group_name, default)

    def items(self) -> Iterable[Tuple[str, Any]]:
        """返回模块名称和实例的动态视图。"""
        return self._modules.items()

    def keys(self) -> Iterable[str]:
        """返回当前已注册模块名称的动态视图。"""
        return self._modules.keys()

    def metadata(self, group_name: str) -> Optional[AdapterMetadata]:
        """
        返回适配器元数据副本，防止调用方修改注册表内部状态。

        Args:
            group_name: 模块标识。

        Returns:
            元数据副本；模块未注册时返回 None。
        """
        value = self._metadata.get(group_name)
        if value is None:
            return None
        return {
            "module": value["module"],
            "mode": value["mode"],
            "contract_version": value["contract_version"],
            "capabilities": list(value["capabilities"]),
            "implementation": value["implementation"],
        }

    @staticmethod
    def _has_methods(module: Any, names: Tuple[str, ...]) -> bool:
        """
        判断实例是否提供一组可调用方法。

        Args:
            module: 待检查适配器实例。
            names: 必须同时存在的方法名。

        Returns:
            所有方法都可调用时返回 True。
        """
        return all(callable(getattr(module, name, None)) for name in names)

    @staticmethod
    def _read_contract_version(module: Any) -> str:
        """
        读取适配器声明的契约版本。

        Args:
            module: 已通过能力检查的适配器实例。

        Returns:
            显式版本字符串；未声明时返回 legacy。
        """
        version = getattr(module, "contract_version", "legacy")
        if callable(version):
            version = version()
        return str(version or "legacy")

    @staticmethod
    def _read_capabilities(module: Any) -> List[str]:
        """
        读取或推断适配器能力列表。

        Args:
            module: 已通过最小能力检查的适配器实例。

        Returns:
            排序后的公开能力名称列表。
        """
        declared = getattr(module, "capabilities", None)
        if callable(declared):
            declared = declared()
        if declared is not None:
            return sorted({str(item) for item in declared})

        # legacy 模块没有能力清单时，只暴露可以实际调用的已知公开方法。
        known = (
            "understand_intent",
            "parse",
            "plan",
            "detect",
            "detect_elements",
            "execute",
            "call_tool",
            "ping",
        )
        return sorted(name for name in known if callable(getattr(module, name, None)))


__all__ = ["AdapterMetadata", "AdapterRegistrationError", "ModuleRegistry"]
