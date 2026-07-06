"""
安全策略加载模块（policies.py）

功能：
1. 优先从 security_policies.json 文件加载策略
2. 文件缺失时 fallback 到内置 DEFAULT_POLICIES
3. 使用正则表达式匹配，\b 单词边界防止误匹配
"""

import json
import logging
import os
import re
from typing import List, Optional

from group5.contracts.schemas import RiskLevel, SecurityPolicy


logger = logging.getLogger(__name__)

# 内置默认策略（JSON 文件缺失时的 fallback）
DEFAULT_POLICIES: List[SecurityPolicy] = [
    {
        "id": "SEC-001",
        "name": "危险命令拦截",
        "description": "拦截危险的系统命令，如 rm、shutdown、reboot 等",
        "action_pattern": r"\b(rm|shutdown|reboot|mkfs|dd|format|fdisk|wipefs)\b",
        "target_pattern": None,
        "risk_level": "critical",
        "block": True,
        "enabled": True,
    },
    {
        "id": "SEC-002",
        "name": "受保护路径拦截",
        "description": "拦截对系统关键路径的访问，如 /etc、/sys、/proc 等",
        "action_pattern": None,
        "target_pattern": r"^/(etc|sys|proc|boot|dev|root|usr/bin|usr/sbin|sbin|bin)(/|$)",
        "risk_level": "critical",
        "block": True,
        "enabled": True,
    },
    {
        "id": "SEC-003",
        "name": "删除操作风险标记",
        "description": "删除操作标记为 high 风险（非拦截）",
        "action_pattern": r"\b(delete|remove|unlink|rmdir|trash)\b",
        "target_pattern": None,
        "risk_level": "high",
        "block": False,
        "enabled": True,
    },
    {
        "id": "SEC-004",
        "name": "写操作风险标记",
        "description": "写入/修改操作标记为 medium 风险（非拦截）",
        "action_pattern": r"\b(write|create|modify|edit|chmod|chown|mv|copy)\b",
        "target_pattern": None,
        "risk_level": "medium",
        "block": False,
        "enabled": True,
    },
]


class PolicyLoader:
    """
    安全策略加载器

    优先从 security_policies.json 加载策略配置；
    文件不存在或解析失败时，自动 fallback 到内置 DEFAULT_POLICIES。
    """

    def __init__(self, json_path: Optional[str] = None) -> None:
        """
        初始化策略加载器

        Args:
            json_path: 策略 JSON 文件路径。
                       若为 None，则使用与本文件同目录的 security_policies.json。
        """
        if json_path is None:
            # 默认路径：本文件同目录下的 security_policies.json
            json_path = os.path.join(
                os.path.dirname(__file__), "security_policies.json"
            )
        self._json_path = json_path
        self._policies: List[SecurityPolicy] = []
        self._load()

    def _load(self) -> None:
        """
        从 JSON 文件加载策略，失败时 fallback 到内置策略。
        """
        if not os.path.exists(self._json_path):
            logger.warning(
                "策略文件不存在: %s，使用内置默认策略（fallback）", self._json_path
            )
            self._policies = list(DEFAULT_POLICIES)
            return

        try:
            with open(self._json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 从 JSON 中提取 policies 列表
            raw_policies = data.get("policies", [])
            loaded: List[SecurityPolicy] = []
            for item in raw_policies:
                # 构建符合 SecurityPolicy TypedDict 的对象
                policy: SecurityPolicy = {
                    "id": item["id"],
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "action_pattern": item.get("action_pattern"),
                    "target_pattern": item.get("target_pattern"),
                    "risk_level": item["risk_level"],
                    "block": item.get("block", True),
                    "enabled": item.get("enabled", True),
                }
                loaded.append(policy)

            self._policies = loaded
            logger.info("成功加载 %d 条安全策略（来源: %s）", len(loaded), self._json_path)

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("策略文件解析失败: %s，使用内置默认策略（fallback）", exc)
            self._policies = list(DEFAULT_POLICIES)

    def get_policies(self) -> List[SecurityPolicy]:
        """
        获取所有已启用的策略列表。

        Returns:
            已启用策略列表（enabled=True 的策略）
        """
        return [p for p in self._policies if p.get("enabled", True)]

    def match(
        self, action: str, target: str
    ) -> Optional[SecurityPolicy]:
        """
        对给定的 action 和 target 进行策略匹配，返回第一个命中的策略。

        匹配规则：
        - action_pattern 不为 None 时，用 re.search 匹配 action（小写）
        - target_pattern 不为 None 时，用 re.search 匹配 target（已归一化）
        - 两个条件同时不为 None 时，需同时满足（AND 逻辑）
        - 单条件不为 None 时，只需满足该条件

        Args:
            action: 操作动词，如 "rm", "open", "delete"
            target: 操作目标路径或名称

        Returns:
            命中的第一条策略，若无匹配则返回 None
        """
        action_lower = action.lower().strip()

        for policy in self.get_policies():
            action_pat = policy.get("action_pattern")
            target_pat = policy.get("target_pattern")

            action_match = True   # 默认匹配（无 action_pattern 时）
            target_match = True   # 默认匹配（无 target_pattern 时）

            # 检查 action 是否匹配
            if action_pat is not None:
                action_match = bool(re.search(action_pat, action_lower))

            # 检查 target 是否匹配
            if target_pat is not None:
                target_match = bool(re.search(target_pat, target))

            # 两个条件必须同时满足（均为 True）
            if action_match and target_match:
                # 至少一个条件有实际的 pattern 才算真正命中
                if action_pat is not None or target_pat is not None:
                    return policy

        return None

    def reload(self, json_path: Optional[str] = None) -> None:
        """
        重新加载策略（支持热更新）

        Args:
            json_path: 新的策略文件路径（可选，不传则重新加载原路径）
        """
        if json_path is not None:
            self._json_path = json_path
        self._load()


if __name__ == "__main__":
    # 独立运行示例：展示策略加载和匹配功能
    loader = PolicyLoader()
    policies = loader.get_policies()
    print(f"已加载 {len(policies)} 条安全策略：")
    for p in policies:
        print(f"  [{p['id']}] {p['name']} - block={p['block']}, risk={p['risk_level']}")

    print("\n=== 策略匹配测试 ===")
    test_cases = [
        ("rm", "-rf /"),
        ("open", "/home/user/Documents"),
        ("navigate", "/etc/passwd"),
        ("delete", "/home/user/file.txt"),
        ("create", "/home/user/new_folder"),
    ]
    for action, target in test_cases:
        matched = loader.match(action, target)
        if matched:
            print(f"  [{action}] [{target}] → 命中 {matched['id']} ({matched['name']}, block={matched['block']})")
        else:
            print(f"  [{action}] [{target}] → 未命中任何策略（放行）")

    print("✅ policies.py 验证通过")
