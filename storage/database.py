"""任务、尝试、阶段和工具调用的SQLite事实存储。"""

import json
import hashlib
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from group5.contracts.schemas import AuditEvent, StageSummaryV1, TaskEnvelopeV1


_VALID_ENVIRONMENTS = {"production", "demo", "test"}
_SCHEMA_VERSION = 4


def resolve_database_path(base_dir: str, environment: str) -> str:
    """
    根据运行环境生成互相隔离的SQLite文件路径。

    Args:
        base_dir: 数据库文件所在的非C盘目录。
        environment: production、demo或test。

    Returns:
        对应环境的绝对数据库路径。

    Raises:
        ValueError: 环境名称不受支持。
    """
    normalized = str(environment).strip().lower()
    if normalized not in _VALID_ENVIRONMENTS:
        raise ValueError(f"不支持的数据库环境: {environment}")
    return os.path.abspath(os.path.join(base_dir, f"group5-{normalized}.sqlite3"))


class SQLiteStore:
    """
    第五组本地SQLite事实存储。

    每个公开写方法都使用独立事务；数据库启用外键、WAL和busy_timeout，
    适用于单机多线程协调器。调用方仍应按任务语义组织跨表提交顺序。
    """

    def __init__(self, database_path: str, environment: str) -> None:
        """
        初始化数据库并创建v1表结构。

        Args:
            database_path: SQLite文件路径；测试可使用`:memory:`。
            environment: production、demo或test。
        """
        normalized = str(environment).strip().lower()
        if normalized not in _VALID_ENVIRONMENTS:
            raise ValueError(f"不支持的数据库环境: {environment}")
        self.database_path = database_path
        self.environment = normalized
        self._lock = threading.RLock()

        if database_path != ":memory:":
            directory = os.path.dirname(os.path.abspath(database_path))
            if directory:
                os.makedirs(directory, exist_ok=True)

        # :memory:数据库必须复用同一连接，否则每次操作会得到独立空库。
        self._memory_connection: Optional[sqlite3.Connection] = None
        if database_path == ":memory:":
            self._memory_connection = self._new_connection(database_path)
        self._active_audit_chain = ""
        self._initialize_schema()

    def record_task_envelope(self, task: TaskEnvelopeV1, status: str) -> None:
        """
        在一个事务中记录任务及当前尝试。

        Args:
            task: 已通过v1运行时校验的任务信封。
            status: 当前任务和尝试状态。
        """
        timestamp = task["timestamp"]
        intent_json = _json_dumps(task["intent"])
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_trace_id, contract_version, environment, status,
                    intent_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_trace_id) DO UPDATE SET
                    status=excluded.status,
                    intent_json=excluded.intent_json,
                    updated_at=excluded.updated_at
                """,
                (
                    task["task_trace_id"],
                    task["contract_version"],
                    self.environment,
                    status,
                    intent_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO attempts (
                    attempt_id, task_trace_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    task["attempt_id"],
                    task["task_trace_id"],
                    status,
                    timestamp,
                    timestamp,
                ),
            )

    def record_stage_summaries(
        self,
        attempt_id: str,
        summaries: List[StageSummaryV1],
    ) -> None:
        """
        以执行顺序保存一次尝试的阶段摘要。

        Args:
            attempt_id: 已存在的尝试ID。
            summaries: 对外结果中的阶段摘要列表。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            for sequence, summary in enumerate(summaries, start=1):
                connection.execute(
                    """
                    INSERT INTO stage_runs (
                        attempt_id, sequence_no, stage_name, status,
                        latency_ms, attempts, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(attempt_id, sequence_no) DO UPDATE SET
                        stage_name=excluded.stage_name,
                        status=excluded.status,
                        latency_ms=excluded.latency_ms,
                        attempts=excluded.attempts
                    """,
                    (
                        attempt_id,
                        sequence,
                        summary["name"],
                        summary["status"],
                        float(summary["latency_ms"]),
                        int(summary["attempts"]),
                        now,
                    ),
                )

    def record_tool_call(
        self,
        tool_call_id: str,
        attempt_id: str,
        tool_name: str,
        status: str,
        arguments: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        保存或更新一次逻辑工具调用。

        Args:
            tool_call_id: 全局唯一工具调用ID。
            attempt_id: 所属尝试ID。
            tool_name: 工具注册名称。
            status: authorized、completed或failed等状态。
            arguments: 经过上层脱敏前的结构化参数。
            result: 可选的标准工具结果。
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls (
                    tool_call_id, attempt_id, tool_name, status,
                    arguments_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                    status=excluded.status,
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (
                    tool_call_id,
                    attempt_id,
                    tool_name,
                    status,
                    _json_dumps(arguments),
                    _json_dumps(result) if result is not None else None,
                    now,
                    now,
                ),
            )

    def get_task_trace(self, task_trace_id: str) -> Optional[Dict[str, Any]]:
        """
        查询任务及其尝试、阶段和工具调用。

        Args:
            task_trace_id: 稳定任务追踪ID。

        Returns:
            聚合后的公开字典；任务不存在时返回None。
        """
        with self._connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_trace_id = ?",
                (task_trace_id,),
            ).fetchone()
            if task is None:
                return None
            attempts = connection.execute(
                "SELECT * FROM attempts WHERE task_trace_id = ? ORDER BY created_at",
                (task_trace_id,),
            ).fetchall()
            attempt_ids = [row["attempt_id"] for row in attempts]
            stages: List[sqlite3.Row] = []
            tools: List[sqlite3.Row] = []
            for attempt_id in attempt_ids:
                stages.extend(
                    connection.execute(
                        "SELECT * FROM stage_runs WHERE attempt_id = ? ORDER BY sequence_no",
                        (attempt_id,),
                    ).fetchall()
                )
                tools.extend(
                    connection.execute(
                        "SELECT * FROM tool_calls WHERE attempt_id = ? ORDER BY created_at",
                        (attempt_id,),
                    ).fetchall()
                )
        return {
            "task": _row_to_dict(task, json_fields={"intent_json"}),
            "attempts": [_row_to_dict(row) for row in attempts],
            "stages": [_row_to_dict(row) for row in stages],
            "tool_calls": [
                _row_to_dict(row, json_fields={"arguments_json", "result_json"})
                for row in tools
            ],
        }

    def append_audit_event(self, event: AuditEvent) -> None:
        """
        追加一条已经脱敏的审计事件。

        Args:
            event: AuditLogger生成并完成脱敏的标准事件。
        """
        payload = event.get("payload", {})
        attempt_id = payload.get("attempt_id") if isinstance(payload, dict) else None
        with self._transaction() as connection:
            last_row = connection.execute(
                """
                SELECT event_hash FROM audit_events
                WHERE chain_id = ? ORDER BY id DESC LIMIT 1
                """,
                (self._active_audit_chain,),
            ).fetchone()
            previous_hash = str(last_row["event_hash"]) if last_row else ""
            event_hash = _audit_event_hash(
                event,
                self._active_audit_chain,
                previous_hash,
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, task_trace_id, attempt_id, event_type,
                    level, message, payload_json, created_at,
                    chain_id, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["trace_id"],
                    attempt_id if isinstance(attempt_id, str) else None,
                    event["event_type"],
                    event["level"],
                    event["message"],
                    _json_dumps(payload),
                    event["timestamp"],
                    self._active_audit_chain,
                    previous_hash,
                    event_hash,
                ),
            )

    def query_audit_by_trace_id(self, task_trace_id: str) -> List[AuditEvent]:
        """
        按任务追踪ID读取审计事件。

        Args:
            task_trace_id: 稳定任务追踪ID。

        Returns:
            按写入顺序排列的标准AuditEvent列表。
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, task_trace_id, event_type, level,
                       message, payload_json, created_at
                FROM audit_events
                WHERE task_trace_id = ?
                ORDER BY id
                """,
                (task_trace_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "trace_id": row["task_trace_id"],
                "event_type": row["event_type"],
                "level": row["level"],
                "message": row["message"],
                "payload": json.loads(row["payload_json"]),
                "timestamp": row["created_at"],
            }
            for row in rows
        ]

    def audit_count(self) -> int:
        """
        返回SQLite中已持久化的审计事件数。

        Returns:
            审计事件总数。
        """
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])

    def verify_audit_chain(self, chain_id: Optional[str] = None) -> Dict[str, Any]:
        """
        校验一个或全部审计链的连续性和内容哈希。

        Args:
            chain_id: 可选链ID；为空时校验数据库中的全部链。

        Returns:
            包含valid、checked_events和首个损坏事件ID的结果。
        """
        with self._connection() as connection:
            if chain_id is None:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY chain_id, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_events WHERE chain_id = ? ORDER BY id",
                    (chain_id,),
                ).fetchall()

        previous_by_chain: Dict[str, str] = {}
        checked = 0
        for row in rows:
            current_chain = str(row["chain_id"] or "")
            expected_previous = previous_by_chain.get(current_chain, "")
            event = _audit_row_to_event(row)
            expected_hash = _audit_event_hash(
                event,
                current_chain,
                expected_previous,
            )
            checked += 1
            if row["previous_hash"] != expected_previous or row["event_hash"] != expected_hash:
                return {
                    "valid": False,
                    "checked_events": checked,
                    "broken_event_id": row["event_id"],
                }
            previous_by_chain[current_chain] = str(row["event_hash"])
        return {
            "valid": True,
            "checked_events": checked,
            "broken_event_id": None,
        }

    def export_audit_jsonl(self, output_path: str) -> Dict[str, Any]:
        """
        导出包含哈希字段的脱敏审计JSONL。

        Args:
            output_path: 导出文件路径。

        Returns:
            导出事件数和导出前完整性校验结果。
        """
        verification = self.verify_audit_chain()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY chain_id, id"
            ).fetchall()
        directory = os.path.dirname(os.path.abspath(output_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as file:
            for row in rows:
                value = dict(row)
                value["payload_json"] = json.loads(value["payload_json"])
                file.write(_json_dumps(value) + "\n")
        return {
            "exported_events": len(rows),
            "verification": verification,
        }

    def rotate_audit_chain(self) -> Dict[str, str]:
        """
        结束当前审计链并创建新链周期。

        Returns:
            旧链ID、旧链最终哈希和新链ID。
        """
        with self._transaction() as connection:
            last = connection.execute(
                """
                SELECT event_hash FROM audit_events
                WHERE chain_id = ? ORDER BY id DESC LIMIT 1
                """,
                (self._active_audit_chain,),
            ).fetchone()
            old_chain = self._active_audit_chain
            final_hash = str(last["event_hash"]) if last else ""
            new_chain = f"{self.environment}:{uuid.uuid4()}"
            metadata_key = f"active_audit_chain:{self.environment}"
            connection.execute(
                "UPDATE store_metadata SET value = ? WHERE key = ?",
                (new_chain, metadata_key),
            )
            self._active_audit_chain = new_chain
        return {
            "old_chain_id": old_chain,
            "old_final_hash": final_hash,
            "new_chain_id": new_chain,
        }

    def prune_closed_audit_chains(self, cutoff_timestamp: str) -> int:
        """
        删除完全早于截止时间的已关闭链周期。

        Args:
            cutoff_timestamp: ISO-8601截止时间；活动链永远不会被删除。

        Returns:
            删除的审计事件数量。
        """
        with self._transaction() as connection:
            candidates = connection.execute(
                """
                SELECT chain_id FROM audit_events
                WHERE chain_id != ?
                GROUP BY chain_id
                HAVING MAX(created_at) < ?
                """,
                (self._active_audit_chain, cutoff_timestamp),
            ).fetchall()
            chain_ids = [row["chain_id"] for row in candidates]
            deleted = 0
            for closed_chain in chain_ids:
                cursor = connection.execute(
                    "DELETE FROM audit_events WHERE chain_id = ?",
                    (closed_chain,),
                )
                deleted += int(cursor.rowcount)
        return deleted

    def upsert_trace_document(self, document: Dict[str, Any]) -> None:
        """
        新增或替换一个任务级RAG文档。

        Args:
            document: 已经过筛选和脱敏的任务文档。
        """
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO trace_documents (
                    task_trace_id, environment, category, is_mock, sensitive,
                    summary, action, target, success, timestamp, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_trace_id) DO UPDATE SET
                    category=excluded.category,
                    is_mock=excluded.is_mock,
                    sensitive=excluded.sensitive,
                    summary=excluded.summary,
                    action=excluded.action,
                    target=excluded.target,
                    success=excluded.success,
                    timestamp=excluded.timestamp,
                    metadata_json=excluded.metadata_json
                """,
                (
                    str(document["task_trace_id"]),
                    self.environment,
                    str(document["category"]),
                    int(bool(document.get("is_mock", False))),
                    int(bool(document.get("sensitive", False))),
                    str(document["summary"]),
                    str(document.get("action", "")),
                    str(document.get("target", "")),
                    int(bool(document.get("success", False))),
                    str(document["timestamp"]),
                    _json_dumps(document.get("metadata", {})),
                ),
            )

    def list_trace_documents(
        self,
        include_mock: bool = False,
        include_sensitive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        读取当前数据库环境允许参与检索的任务文档。

        Args:
            include_mock: 是否包含标记为Mock的文档。
            include_sensitive: 是否包含敏感文档；生产查询默认禁止。

        Returns:
            按时间排序的任务文档列表。
        """
        clauses = ["environment = ?"]
        params: List[Any] = [self.environment]
        if not include_mock:
            clauses.append("is_mock = 0")
        if not include_sensitive:
            clauses.append("sensitive = 0")
        query = (
            "SELECT * FROM trace_documents WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp"
        )
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "task_trace_id": row["task_trace_id"],
                "environment": row["environment"],
                "category": row["category"],
                "is_mock": bool(row["is_mock"]),
                "sensitive": bool(row["sensitive"]),
                "summary": row["summary"],
                "action": row["action"],
                "target": row["target"],
                "success": bool(row["success"]),
                "timestamp": row["timestamp"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def delete_trace_document(self, task_trace_id: str) -> bool:
        """
        删除用户指定任务的派生RAG文档，不修改审计事件。

        Args:
            task_trace_id: 待清除的任务追踪ID。

        Returns:
            实际删除文档时返回True。
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM trace_documents WHERE task_trace_id = ?",
                (task_trace_id,),
            )
            return cursor.rowcount > 0

    def ping(self) -> Dict[str, Any]:
        """
        检查数据库连接和模式版本。

        Returns:
            包含healthy、environment和schema_version的状态字典。
        """
        try:
            with self._connection() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                connection.execute("SELECT 1").fetchone()
            return {
                "healthy": version == _SCHEMA_VERSION,
                "environment": self.environment,
                "schema_version": version,
            }
        except sqlite3.Error as exc:
            return {
                "healthy": False,
                "environment": self.environment,
                "error": str(exc),
            }

    def close(self) -> None:
        """关闭内存数据库持有的长期连接。"""
        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None

    def _initialize_schema(self) -> None:
        """创建v1表、索引和模式版本。"""
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_trace_id TEXT PRIMARY KEY,
                    contract_version TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    status TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_trace_id TEXT NOT NULL REFERENCES tasks(task_trace_id),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    sequence_no INTEGER NOT NULL,
                    stage_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, sequence_no)
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    tool_call_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    task_trace_id TEXT NOT NULL,
                    attempt_id TEXT,
                    event_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    chain_id TEXT,
                    previous_hash TEXT,
                    event_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS trace_documents (
                    task_trace_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    category TEXT NOT NULL,
                    is_mock INTEGER NOT NULL,
                    sensitive INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_task
                    ON attempts(task_trace_id);
                CREATE INDEX IF NOT EXISTS idx_stage_runs_attempt
                    ON stage_runs(attempt_id, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_attempt
                    ON tool_calls(attempt_id);
                CREATE INDEX IF NOT EXISTS idx_audit_events_trace
                    ON audit_events(task_trace_id, id);
                CREATE INDEX IF NOT EXISTS idx_trace_documents_environment
                    ON trace_documents(environment, is_mock, sensitive);
                """
            )
            # 兼容Ticket 14创建的v2数据库；ALTER仅在缺列时执行，不删除或
            # 重建任何既有审计数据。
            _ensure_column(connection, "audit_events", "chain_id", "TEXT")
            _ensure_column(connection, "audit_events", "previous_hash", "TEXT")
            _ensure_column(connection, "audit_events", "event_hash", "TEXT")

            metadata_key = f"active_audit_chain:{self.environment}"
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?",
                (metadata_key,),
            ).fetchone()
            if row is None:
                self._active_audit_chain = (
                    f"{self.environment}:{uuid.uuid4()}"
                )
                connection.execute(
                    "INSERT INTO store_metadata (key, value) VALUES (?, ?)",
                    (metadata_key, self._active_audit_chain),
                )
            else:
                self._active_audit_chain = str(row["value"])

            _backfill_audit_hashes(connection, self._active_audit_chain)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _new_connection(self, path: str) -> sqlite3.Connection:
        """
        创建配置一致的SQLite连接。

        Args:
            path: 数据库文件路径或:memory:。

        Returns:
            已启用外键和行对象的连接。
        """
        connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _connection(self):
        """返回可作为上下文管理器使用的连接。"""
        if self._memory_connection is not None:
            return _LockedConnection(self._memory_connection, self._lock)
        return _OwnedConnection(self._new_connection(self.database_path))

    def _transaction(self):
        """返回带提交和回滚语义的线程安全事务上下文。"""
        return _Transaction(self._connection(), self._lock)


class _LockedConnection:
    """为共享内存连接提供线程锁的内部上下文管理器。"""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        """保存共享连接和锁。"""
        self._connection_value = connection
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        """加锁并返回共享连接。"""
        self._lock.acquire()
        return self._connection_value

    def __exit__(self, exc_type, exc, traceback) -> None:
        """退出读取上下文并释放锁。"""
        self._lock.release()


class _OwnedConnection:
    """确保单次文件数据库连接在上下文结束时关闭。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """保存本上下文独占的SQLite连接。"""
        self._connection_value = connection

    def __enter__(self) -> sqlite3.Connection:
        """返回独占连接。"""
        return self._connection_value

    def __exit__(self, exc_type, exc, traceback) -> None:
        """无论成功或失败都关闭连接。"""
        self._connection_value.close()


class _Transaction:
    """统一文件连接和共享内存连接的事务生命周期。"""

    def __init__(self, context, lock: threading.RLock) -> None:
        """保存连接上下文；lock参数保留接口一致性。"""
        self._context = context
        self._connection_value: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        """进入连接上下文并显式开始事务。"""
        self._connection_value = self._context.__enter__()
        self._connection_value.execute("BEGIN IMMEDIATE")
        return self._connection_value

    def __exit__(self, exc_type, exc, traceback) -> None:
        """成功时提交，异常时回滚，并关闭或释放连接。"""
        if self._connection_value is not None:
            if exc_type is None:
                self._connection_value.commit()
            else:
                self._connection_value.rollback()
        self._context.__exit__(exc_type, exc, traceback)


def _json_dumps(value: Any) -> str:
    """将JSON值序列化为稳定紧凑文本。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _row_to_dict(
    row: sqlite3.Row,
    json_fields: Optional[set] = None,
) -> Dict[str, Any]:
    """
    将SQLite行转换为普通字典并解析指定JSON字段。

    Args:
        row: SQLite Row对象。
        json_fields: 需要反序列化的字段集合。

    Returns:
        可安全交给调用方的普通字典。
    """
    result = dict(row)
    for field in json_fields or set():
        if result.get(field) is not None:
            result[field] = json.loads(result[field])
    return result


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """
    为旧模式安全补充缺失列。

    Args:
        connection: 当前模式迁移事务连接。
        table: 已知内部表名。
        column: 需要确认的列名。
        declaration: SQLite列类型声明。
    """
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


def _audit_event_hash(
    event: AuditEvent,
    chain_id: str,
    previous_hash: str,
) -> str:
    """
    计算单条审计事件在指定链中的哈希。

    Args:
        event: 标准脱敏审计事件。
        chain_id: 当前链周期ID。
        previous_hash: 前一事件哈希，链首为空字符串。

    Returns:
        SHA-256十六进制摘要。
    """
    canonical_event = _json_dumps(
        {
            "event_id": event["event_id"],
            "trace_id": event["trace_id"],
            "event_type": event["event_type"],
            "level": event["level"],
            "message": event["message"],
            "payload": event["payload"],
            "timestamp": event["timestamp"],
        }
    )
    material = f"{chain_id}|{previous_hash}|{canonical_event}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _audit_row_to_event(row: sqlite3.Row) -> AuditEvent:
    """
    将audit_events行恢复为参与哈希的标准事件。

    Args:
        row: SQLite审计事件行。

    Returns:
        标准AuditEvent对象。
    """
    return {
        "event_id": row["event_id"],
        "trace_id": row["task_trace_id"],
        "event_type": row["event_type"],
        "level": row["level"],
        "message": row["message"],
        "payload": json.loads(row["payload_json"]),
        "timestamp": row["created_at"],
    }


def _backfill_audit_hashes(
    connection: sqlite3.Connection,
    fallback_chain_id: str,
) -> None:
    """
    为v2审计数据补齐链ID和哈希。

    Args:
        connection: 当前模式迁移事务连接。
        fallback_chain_id: 无链ID旧事件归入的当前链。
    """
    rows = connection.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
    previous_by_chain: Dict[str, str] = {}
    for row in rows:
        chain_id = str(row["chain_id"] or fallback_chain_id)
        if row["chain_id"] and row["event_hash"] is not None:
            # 已带哈希的事件绝不在启动时自动修复，否则删除或修改历史后
            # 重新启动可能掩盖篡改证据。
            previous_by_chain[chain_id] = str(row["event_hash"])
            continue
        previous_hash = previous_by_chain.get(chain_id, "")
        event_hash = _audit_event_hash(
            _audit_row_to_event(row),
            chain_id,
            previous_hash,
        )
        connection.execute(
            """
            UPDATE audit_events
            SET chain_id = ?, previous_hash = ?, event_hash = ?
            WHERE id = ?
            """,
            (chain_id, previous_hash, event_hash, row["id"]),
        )
        previous_by_chain[chain_id] = event_hash


__all__ = ["SQLiteStore", "resolve_database_path"]
