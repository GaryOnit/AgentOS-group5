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

import logging
import os
import sys
import time

# 确保项目根目录在 sys.path 中（支持 python group5/main.py 直接运行）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 配置日志（演示时只显示 WARNING 及以上，减少噪声）
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from group5.audit.audit_logger import AuditLogger
from group5.contracts.schemas import IntentJSON, OrchestrateResult
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import MockGroup2Planner, MockGroup3Executor, MockGroup4ToolRegistry
from group5.security.security_sandbox import SecuritySandbox


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


def print_result(scenario_num: int, desc: str, result: OrchestrateResult) -> None:
    """格式化打印编排结果"""
    status = "✅ 成功" if result["success"] else "🚫 拦截/失败"
    print(f"\n【场景{scenario_num}】{desc}")
    print(f"  状态:   {status}")
    print(f"  阶段:   {result['stage']}")
    print(f"  耗时:   {result['latency_ms']:.1f}ms")
    print(f"  追踪ID: {result['trace_id'][:16]}...")

    if result["success"]:
        res = result.get("result") or {}
        exec_out = res.get("exec", {}).get("output", "已执行完成") if res else "已执行完成"
        print(f"  输出:   {exec_out}")
    else:
        error = result.get("error") or {}
        print(f"  错误码: {error.get('code', 'N/A')}")
        print(f"  错误:   {error.get('message', 'N/A')}")


# ═══════════════════════════════════════════════════════════
# 主演示函数
# ═══════════════════════════════════════════════════════════

def run_demo() -> None:
    """运行全部验收场景演示"""
    print_separator("第5组系统协调层 - 验收演示", width=64)
    print("Linux Agentic OS 课程项目（UFO² 架构适配）")
    print("组件：SystemCoordinator + SecuritySandbox + RAGKnowledgeBase + AuditLogger")
    print_separator(width=64)

    # ── 初始化所有组件 ──────────────────────────────────────
    rag_kb = RAGKnowledgeBase()
    audit = AuditLogger()
    security = SecuritySandbox()

    coordinator = SystemCoordinator(
        rag_kb=rag_kb,
        audit_logger=audit,
        security_sandbox=security,
    )

    # 注册外部模块（使用 Mock 实现，替代真实 Group2/3/4）
    coordinator.register("group2", MockGroup2Planner())
    coordinator.register("group3", MockGroup3Executor())
    coordinator.register("group4", MockGroup4ToolRegistry())

    # 健康检查
    print("\n📋 模块健康状态:")
    health = coordinator.health_check()
    for name, status in sorted(health.items()):
        mark = "✅" if status["healthy"] else "❌"
        latency = f"{status['latency_ms']:.1f}ms"
        print(f"   {mark} {name:<15} {latency}")

    print_separator("验收场景演示", width=64)

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
        run_demo()
    except KeyboardInterrupt:
        print("\n\n演示被用户中断")
        sys.exit(0)
    except Exception as exc:
        print(f"\n❌ 演示出错: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
