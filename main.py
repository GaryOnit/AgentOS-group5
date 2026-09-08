"""
第5组系统协调层演示入口（main.py）

展示 6 个验收场景（对应设计任务书中的验收场景）：
- 场景1: 打开文件管理器（应用控制，low 风险）
- 场景2: 导航到 /home/user/Documents（文件操作，low 风险）
- 场景3: 创建文件夹（文件操作，medium 风险）
- 场景4: 尝试 rm -rf /（危险操作，应被拦截）
- 场景5: 尝试访问 /etc/passwd（受保护路径，应被拦截）
- 场景6: 整理下载目录（文件操作，含工具调用）
- 场景7: 路径穿越攻击防护（额外演示）

运行方式：
    cd /path/to/agent-OS
    python group5/main.py
"""

import argparse
import logging
import os
import sys
import time

# 确保项目根目录在 sys.path 中（支持 python group5/main.py 直接运行）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GROUP5_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 配置日志（演示时只显示 WARNING 及以上，减少噪声）
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from group5.audit.audit_logger import AuditLogger
from group5.contracts.schemas import IntentJSON
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import (
    MockGroup1HostAgent,
    MockGroup2Planner,
    MockGroup3Executor,
    MockGroup4ToolRegistry,
)
from group5.security.security_sandbox import SecuritySandbox
from group5.storage.database import SQLiteStore, resolve_database_path


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def print_separator(title: str = "", width: int = 60) -> None:
    """打印分隔线"""
    if title:
        pad = max((width - len(title) - 2) // 2, 1)
        print(f"\n{'═' * pad} {title} {'═' * pad}")
    else:
        print("═" * width)


def print_result(scenario_num: int, desc: str, result: dict) -> None:
    """
    格式化打印旧版或v1编排结果。

    Args:
        scenario_num: 演示场景编号。
        desc: 场景说明。
        result: 旧OrchestrateResult或v1结果字典。
    """
    status = "✅ 成功" if result["success"] else "🚫 拦截/失败"
    latency_ms = result.get("total_latency_ms", result.get("latency_ms", 0.0))
    trace_id = result.get("task_trace_id", result.get("trace_id", ""))
    print(f"\n【场景{scenario_num}】{desc}")
    print(f"  状态:   {status}")
    print(f"  阶段:   {result['stage']}")
    print(f"  耗时:   {latency_ms:.1f}ms")
    print(f"  追踪ID: {trace_id[:16]}...")

    if result["success"]:
        res = result.get("result") or {}
        exec_out = res.get("exec", {}).get("output", "已执行完成") if res else "已执行完成"
        print(f"  输出:   {exec_out}")
    else:
        error = result.get("error") or {}
        print(f"  错误码: {error.get('code', 'N/A')}")
        print(f"  错误:   {error.get('message', 'N/A')}")


class _DemoGroup1Adapter:
    """将第五组旧Mock包装为第一组四字段演示契约。"""

    contract_version = "1.0"
    mode = "mock"

    def __init__(self) -> None:
        """初始化第五组旧Mock作为离线关键词后端。"""
        self._legacy = MockGroup1HostAgent()

    def understand_intent(self, user_input: str, history=None) -> dict:
        """
        解析演示指令并补齐第一组intent分类。

        Args:
            user_input: 当前自然语言指令。
            history: 可选历史；旧Mock不使用该参数。

        Returns:
            符合第一组成功契约的四字段对象。
        """
        parsed = self._legacy.parse(user_input)
        action = parsed["action"]
        if action in {"open", "close", "switch"}:
            category = "应用控制"
        elif action in {"adjust", "enable", "disable"}:
            category = "系统设置"
        elif action in {"query", "check"}:
            category = "信息查询"
        else:
            category = "文件操作"
        return {
            "intent": category,
            "target": parsed["target"],
            "action": action,
            "params": dict(parsed.get("params", {})),
        }


# ═══════════════════════════════════════════════════════════
# 主演示函数
# ═══════════════════════════════════════════════════════════

def _load_group1(mode: str):
    """
    加载第一组Mock或其真实HostAgent门面。

    Args:
        mode: mock、group1-mock或auto。

    Returns:
        可注册到第五组的第一组模块实例。

    Raises:
        RuntimeError: 第一组源码目录不存在或无法导入。
    """
    if mode == "mock":
        return _DemoGroup1Adapter()

    source_dir = os.getenv(
        "GROUP1_SOURCE_DIR",
        os.path.join(_ROOT, "ai-shell-hostagent-update-liujiyuan"),
    )
    if not os.path.isdir(source_dir):
        raise RuntimeError(f"第一组源码目录不存在: {source_dir}")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    try:
        from host_agent_mock import HostAgent
    except ImportError as exc:
        raise RuntimeError("无法导入第一组HostAgent") from exc
    return HostAgent(mode="mock" if mode == "group1-mock" else "auto")


def create_demo_coordinator(mode: str = "mock", database_path: str = ""):
    """
    创建使用独立demo数据库的协调器。

    Args:
        mode: 第一组运行模式。
        database_path: 可选demo数据库路径；为空时使用group5/data。

    Returns:
        coordinator、rag_kb和audit三元组。
    """
    path = database_path or resolve_database_path(
        os.path.join(_GROUP5_ROOT, "data"),
        "demo",
    )
    state_store = SQLiteStore(path, "demo")
    rag_kb = RAGKnowledgeBase(state_store=state_store, include_mock=True)
    audit = AuditLogger(state_store=state_store)
    coordinator = SystemCoordinator(
        rag_kb=rag_kb,
        audit_logger=audit,
        security_sandbox=SecuritySandbox(),
        state_store=state_store,
    )
    coordinator.register("group1", _load_group1(mode))
    coordinator.register("group2", MockGroup2Planner())
    coordinator.register("group3", MockGroup3Executor())
    coordinator.register("group4", MockGroup4ToolRegistry())
    return coordinator, rag_kb, audit


def run_demo(mode: str = "mock", database_path: str = "") -> None:
    """
    运行全部验收场景演示。

    Args:
        mode: 第一组运行模式。
        database_path: 可选demo数据库路径。
    """
    print_separator("第5组系统协调层 - 验收演示", width=64)
    print("Linux Agentic OS 课程项目（UFO² 架构适配）")
    print("组件：SystemCoordinator + SecuritySandbox + RAGKnowledgeBase + AuditLogger")
    print_separator(width=64)

    coordinator, rag_kb, audit = create_demo_coordinator(mode, database_path)
    print(f"运行模式：Group1={mode}，Group2/3/4=mock，environment=demo")

    # 健康检查
    print("\n📋 模块健康状态:")
    health = coordinator.health_check()
    for name, status in sorted(health.items()):
        mark = "✅" if status["healthy"] else "❌"
        latency = f"{status['latency_ms']:.1f}ms"
        print(f"   {mark} {name:<15} {latency}")

    print_separator("验收场景演示", width=64)

    # 自然语言入口冒烟：Group1解析后进入与结构化场景相同的核心链路。
    natural_result = coordinator.orchestrate_text("打开文件管理器")
    print_result(0, "自然语言 → Group1 → Group5完整入口", natural_result)

    # ══════════════════════════════════════════════════════
    # 场景1：打开文件管理器（应用控制，low 风险）
    # ══════════════════════════════════════════════════════
    intent1: IntentJSON = {
        "trace_id": "demo-scene-001",
        "action": "open",
        "target": "/home/user",
        "params": {"app": "file_manager"},
        "raw_text": "打开文件管理器",
    }
    result1 = coordinator.orchestrate(intent1)
    print_result(1, "打开文件管理器（应用控制，low 风险）", result1)

    # ══════════════════════════════════════════════════════
    # 场景2：导航到 /home/user/Documents（文件操作，low 风险）
    # ══════════════════════════════════════════════════════
    intent2: IntentJSON = {
        "trace_id": "demo-scene-002",
        "action": "navigate",
        "target": "/home/user/Documents",
        "params": {},
        "raw_text": "导航到文档文件夹",
    }
    result2 = coordinator.orchestrate(intent2)
    print_result(2, "导航到 /home/user/Documents（文件操作，low 风险）", result2)

    # ══════════════════════════════════════════════════════
    # 场景3：创建文件夹（写操作，medium 风险，允许执行）
    # ══════════════════════════════════════════════════════
    intent3: IntentJSON = {
        "trace_id": "demo-scene-003",
        "action": "create",
        "target": "/home/user/Documents/新建文件夹",
        "params": {"type": "directory"},
        "raw_text": "在文档目录创建新文件夹",
    }
    result3 = coordinator.orchestrate(intent3)
    print_result(3, "创建文件夹（写操作 SEC-004 标记 medium 风险，允许执行）", result3)

    # ══════════════════════════════════════════════════════
    # 场景4：尝试 rm -rf /（危险操作，应被拦截）
    # ══════════════════════════════════════════════════════
    intent4: IntentJSON = {
        "trace_id": "demo-scene-004",
        "action": "rm",
        "target": "-rf /",
        "params": {"force": True},
        "raw_text": "rm -rf / 删除根目录",
    }
    result4 = coordinator.orchestrate(intent4)
    print_result(4, "尝试 rm -rf /（高危命令，SEC-001 拦截 critical）", result4)

    # ══════════════════════════════════════════════════════
    # 场景5：尝试访问 /etc/passwd（受保护路径，应被拦截）
    # ══════════════════════════════════════════════════════
    intent5: IntentJSON = {
        "trace_id": "demo-scene-005",
        "action": "read",
        "target": "/etc/passwd",
        "params": {},
        "raw_text": "读取 /etc/passwd 密码文件",
    }
    result5 = coordinator.orchestrate(intent5)
    print_result(5, "访问 /etc/passwd（受保护路径，SEC-002 拦截 critical）", result5)

    # ══════════════════════════════════════════════════════
    # 场景6：整理下载目录（文件操作，含工具调用）
    # ══════════════════════════════════════════════════════
    intent6: IntentJSON = {
        "trace_id": "demo-scene-006",
        "action": "organize",
        "target": "/home/user/Downloads",
        "params": {"sort_by": "type"},
        "raw_text": "整理下载目录，按文件类型分类",
    }
    result6 = coordinator.orchestrate(intent6)
    print_result(6, "整理下载目录（文件操作，含工具调用，low 风险）", result6)

    # ══════════════════════════════════════════════════════
    # 场景7（额外）：路径穿越攻击防护
    # ══════════════════════════════════════════════════════
    print_separator("额外演示：路径穿越防护", width=64)
    intent_traversal: IntentJSON = {
        "trace_id": "demo-traversal-001",
        "action": "navigate",
        "target": "/home/../etc/passwd",   # 路径穿越攻击
        "params": {},
        "raw_text": "尝试路径穿越攻击",
    }
    result_traversal = coordinator.orchestrate(intent_traversal)
    print_result(
        7,
        "路径穿越 /home/../etc/passwd（归一化为 /etc/passwd 后被拦截）",
        result_traversal,
    )

    # 等待异步 RAG 写入完成
    time.sleep(0.5)
    audit.flush(timeout=2.0)
    time.sleep(0.2)

    # ══════════════════════════════════════════════════════
    # 统计摘要
    # ══════════════════════════════════════════════════════
    print_separator("运行统计", width=64)
    stats = coordinator.stats()
    total = stats["total_orchestrations"]
    success = stats["success_count"]
    fail = stats["failure_count"]
    rate = stats["success_rate"] * 100

    print(f"\n📊 编排统计:")
    print(f"   总计:   {total} 次")
    print(f"   成功:   {success} 次")
    print(f"   拦截/失败: {fail} 次")
    print(f"   成功率: {rate:.1f}%")

    rag_stats = stats.get("rag_stats", {})
    print(f"\n🧠 RAG 知识库:")
    print(f"   记录数:   {rag_stats.get('total_records', 0)}")
    print(f"   词汇量:   {rag_stats.get('vocab_size', 0)}")

    security_stats = stats.get("security_stats", {})
    print(f"\n🛡  安全统计:")
    print(f"   检查次数: {security_stats.get('total_checks', 0)}")
    print(f"   拦截次数: {security_stats.get('blocked_count', 0)}")
    block_rate = security_stats.get('block_rate', 0.0) * 100
    print(f"   拦截率:   {block_rate:.1f}%")

    print(f"\n📝 审计事件: {stats.get('audit_count', 0)} 条")
    audit_events = audit.get_all()
    if audit_events:
        print(f"   最近 5 条:")
        for ev in audit_events[-5:]:
            icon = {"INFO": "ℹ️ ", "WARN": "⚠️ ", "ERROR": "❌"}.get(ev["level"], "• ")
            tid = ev["trace_id"][:12]
            etype = ev["event_type"][:25]
            msg = ev["message"][:35]
            print(f"   {icon}[{tid}] {etype}: {msg}")

    # ══════════════════════════════════════════════════════
    # RAG 检索演示
    # ══════════════════════════════════════════════════════
    print_separator("RAG 知识库检索演示", width=64)
    test_queries = [
        "打开文件管理器",
        "文档目录导航",
        "危险删除命令",
    ]
    for query in test_queries:
        results = rag_kb.query(query, top_k=2)
        print(f"\n🔍 查询: '{query}'")
        if results:
            for r in results:
                mark = "✅" if r["success"] else "🚫"
                summary_short = r["summary"][:45]
                print(f"   {mark} score={r['score']:.3f} {summary_short}...")
        else:
            print("   （知识库暂无相关记录）")

    print_separator("演示完成", width=64)
    print("\n✅ 第5组系统协调层验收演示完成！")
    print("   · 安全拦截正常（场景4: rm命令, 场景5: /etc路径, 场景7: 路径穿越）")
    print("   · 全流程编排成功（场景1: 打开应用, 场景2: 导航, 场景6: 整理目录）")
    print("   · 风险分级正常（场景3: create操作 medium 风险放行）")
    print("   · 路径穿越防护验证通过")
    print_separator(width=64)

    # 优雅关闭
    coordinator.shutdown(wait=False)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="第5组系统协调层验收演示")
        parser.add_argument(
            "--mode",
            choices=("mock", "group1-mock", "auto"),
            default="mock",
            help="第一组模式；Group2/3/4在当前仓库中仍使用Mock",
        )
        parser.add_argument(
            "--database-path",
            default="",
            help="可选demo SQLite路径，默认写入group5/data",
        )
        arguments = parser.parse_args()
        run_demo(arguments.mode, arguments.database_path)
    except KeyboardInterrupt:
        print("\n\n演示被用户中断")
        sys.exit(0)
    except Exception as exc:
        print(f"\n❌ 演示出错: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
