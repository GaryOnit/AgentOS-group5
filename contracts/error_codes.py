"""
错误码定义模块（error_codes.py）

统一定义系统所有错误码、错误描述和工厂函数。
错误码规范：
    E1xxx: 安全类错误
    E2xxx: 规划类错误
    E3xxx: 执行类错误
    E4xxx: 工具调用类错误
    E5xxx: 系统类错误
"""

from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    """
    错误码枚举

    继承 str 使枚举值可直接用于字符串比较和 JSON 序列化。
    """
    # 安全类错误（E1xxx）
    E1001 = "E1001"   # 安全检查拒绝（危险操作/受保护路径）
    E1002 = "E1002"   # 路径穿越攻击检测

    # 规划类错误（E2xxx）
    E2001 = "E2001"   # 规划模块未注册/不可用
    E2002 = "E2002"   # 规划结果格式无效

    # 执行类错误（E3xxx）
    E3001 = "E3001"   # 执行模块未注册/不可用
    E3002 = "E3002"   # 执行结果格式无效

    # 工具调用类错误（E4xxx）
    E4001 = "E4001"   # 工具注册表未注册/不可用
    E4002 = "E4002"   # 工具调用失败

    # 系统类错误（E5xxx）
    E5001 = "E5001"   # 输入参数校验失败
    E5002 = "E5002"   # 模块调用超时（超过 3s）
    E5003 = "E5003"   # 未知内部错误


# 错误码对应的中文描述字典
ERROR_MESSAGES: Dict[ErrorCode, str] = {
    # 安全类
    ErrorCode.E1001: "安全检查拒绝：检测到危险操作或受保护路径访问",
    ErrorCode.E1002: "安全检查拒绝：检测到路径穿越攻击（../ 序列）",
    # 规划类
    ErrorCode.E2001: "规划模块（Group2）未注册或服务不可用",
    ErrorCode.E2002: "规划模块返回结果格式无效，缺少必要字段",
    # 执行类
    ErrorCode.E3001: "执行模块（Group3）未注册或服务不可用",
    ErrorCode.E3002: "执行模块返回结果格式无效，缺少必要字段",
    # 工具类
    ErrorCode.E4001: "工具注册表（Group4）未注册或服务不可用",
    ErrorCode.E4002: "工具调用失败，工具执行过程中发生错误",
    # 系统类
    ErrorCode.E5001: "输入参数校验失败：缺少必要字段或类型错误",
    ErrorCode.E5002: "模块调用超时：响应时间超过 3 秒阈值",
    ErrorCode.E5003: "系统内部未知错误，请查看审计日志排查",
}


def make_error(
    code: ErrorCode,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    错误信息工厂函数

    构造符合接口契约的错误字典，用于 OrchestrateResult.error 字段。

    Args:
        code: 错误码枚举值
        detail: 可选的详细错误描述（覆盖默认描述）
        extra: 可选的额外数据字典（如触发路径、违规策略等）

    Returns:
        格式化的错误字典：
        {
            "code": "E1001",
            "message": "...",
            "detail": "...",
            ...extra
        }

    Example:
        >>> err = make_error(ErrorCode.E1001, detail="尝试执行 rm -rf /")
        >>> print(err)
        {'code': 'E1001', 'message': '安全检查拒绝：...', 'detail': '尝试执行 rm -rf /'}
    """
    error: Dict[str, Any] = {
        "code": code.value,                            # 错误码字符串，如 "E1001"
        "message": ERROR_MESSAGES.get(code, "未知错误"),  # 错误描述
    }

    # 如果有额外详情，追加到错误字典
    if detail is not None:
        error["detail"] = detail

    # 如果有额外字段，合并到错误字典（不覆盖已有字段）
    if extra:
        for k, v in extra.items():
            if k not in error:
                error[k] = v

    return error


if __name__ == "__main__":
    # 独立运行示例：展示错误码和工厂函数用法
    print("=== 错误码列表 ===")
    for code in ErrorCode:
        print(f"  {code.value}: {ERROR_MESSAGES[code]}")

    print("\n=== make_error 示例 ===")
    err1 = make_error(ErrorCode.E1001, detail="尝试执行 rm -rf /")
    print("E1001 错误:", err1)

    err2 = make_error(
        ErrorCode.E5002,
        detail="Group2 Planner 响应超时",
        extra={"module": "group2_planner", "timeout_s": 3.0}
    )
    print("E5002 错误:", err2)

    print("✅ error_codes.py 验证通过")
