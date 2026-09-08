"""第五组跨组适配器协议与可信注册入口。"""

from group5.adapters.protocols import (
    Group1Adapter,
    Group2Adapter,
    Group3Adapter,
    Group4Adapter,
)
from group5.adapters.group1 import Group1AdapterError, adapt_group1_intent
from group5.adapters.group2 import (
    Group2AdapterError,
    adapt_group2_detection,
    adapt_group2_plan,
    build_legacy_execution_plan,
)
from group5.adapters.group3 import Group3AdapterError, adapt_group3_execute
from group5.adapters.group4 import (
    Group4AdapterError,
    adapt_group4_call,
    normalize_tool_request,
)
from group5.adapters.registry import (
    AdapterMetadata,
    AdapterRegistrationError,
    ModuleRegistry,
)


__all__ = [
    "AdapterMetadata",
    "AdapterRegistrationError",
    "Group1Adapter",
    "Group1AdapterError",
    "Group2Adapter",
    "Group2AdapterError",
    "Group3Adapter",
    "Group3AdapterError",
    "Group4Adapter",
    "Group4AdapterError",
    "ModuleRegistry",
    "adapt_group1_intent",
    "adapt_group2_detection",
    "adapt_group2_plan",
    "build_legacy_execution_plan",
    "adapt_group3_execute",
    "adapt_group4_call",
    "normalize_tool_request",
]
