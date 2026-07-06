# Group5 系统协调层（System Coordinator + Security + RAG）

本目录是第 5 组实现，负责：
- `SystemCoordinator`：跨组编排（安全检查 → 规划 → 执行 → 工具调用 → 入库）
- `SecuritySandbox`：四层安全检查（权限检查 / 沙箱执行检查 / 签名验证 / 隐私保护）
- `RAGKnowledgeBase`：执行轨迹存储与检索（TF-IDF）
- `AuditLogger`：审计日志异步记录

---

## 1. 目录结构

```text
group5/
  coordinator/system_coordinator.py
  security/security_sandbox.py
  security/security_policies.json
  knowledge/rag_kb.py
  knowledge/trace_store.py
  audit/audit_logger.py
  contracts/schemas.py
  contracts/error_codes.py
  mocks/mock_modules.py
  tests/
  main.py
```

---

## 2. 运行环境说明

课程项目主运行环境为 Linux（Ubuntu 22.04+）。
为了便于本地开发和测试，本目录同时提供 macOS / Windows 的执行指南。

### 2.1 Python 版本

建议：`Python 3.10+`（推荐 3.11/3.12）

### 2.2 依赖

最小依赖：
- `numpy`

安装命令：

```bash
python3 -m pip install numpy
```

如果系统里有多个 Python，请显式使用对应解释器：

```bash
python3.11 -m pip install numpy
```

---

## 3. Linux 运行指南（主环境）

### 3.1 安装依赖

```bash
cd /path/to/agent-OS
python3 -m pip install numpy
```

### 3.2 运行演示

```bash
python3 group5/main.py
```

### 3.3 运行测试

```bash
python3 -m unittest discover -s group5/tests -p "test_*.py"
```

---

## 4. macOS 运行指南

### 4.1 安装依赖

```bash
cd /path/to/agent-OS
python3 -m pip install numpy
```

如果出现 `pip` 不可用：

```bash
python3 -m ensurepip --upgrade
python3 -m pip install --upgrade pip
python3 -m pip install numpy
```

### 4.2 运行演示

```bash
python3 group5/main.py
```

### 4.3 运行测试

```bash
python3 -m unittest discover -s group5/tests -p "test_*.py"
```

---

## 5. Windows 运行指南

以下命令在 `PowerShell` 执行。

### 5.1 安装依赖

```powershell
cd C:\path\to\agent-OS
py -3 -m pip install numpy
```

### 5.2 运行演示

```powershell
py -3 group5/main.py
```

### 5.3 运行测试

```powershell
py -3 -m unittest discover -s group5/tests -p "test_*.py"
```

---

## 6. 签名验证（可选）

`SecuritySandbox` 第三层支持请求签名校验：
- 未提供 `params.signature`：兼容模式放行
- 提供了 `params.signature`：必须通过 HMAC-SHA256 校验

默认密钥来源：
- 环境变量 `GROUP5_SIGNATURE_SECRET`
- 若未设置，使用开发默认值 `group5-dev-secret`

Linux/macOS 设置示例：

```bash
export GROUP5_SIGNATURE_SECRET="your-secret"
```

Windows PowerShell 设置示例：

```powershell
$env:GROUP5_SIGNATURE_SECRET = "your-secret"
```

---

## 7. 常见问题

### 7.1 `ModuleNotFoundError: No module named 'numpy'`

说明缺少依赖，安装即可：

```bash
python3 -m pip install numpy
```

### 7.2 只想调试安全模块，但导入时触发其他模块依赖

请直接从子模块导入，不要依赖 `group5` 顶层导出：

```python
from group5.security.security_sandbox import SecuritySandbox
```

---

## 8. 课程验收相关建议

- 先运行 `group5/main.py` 演示 6 个验收场景 + 额外路径穿越场景。
- 再运行 `group5/tests` 输出测试结果截图。
- 提交时附上当前 OS 和 Python 版本，便于复现。