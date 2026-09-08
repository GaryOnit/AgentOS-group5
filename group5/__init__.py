"""
第五组独立仓库的包路径兼容入口。

用途说明：
    最终集成目录中，本仓库应以 ``group5`` 作为目录名放在系统根目录下；
    但独立克隆时仓库目录通常名为 ``AgentOS-group5``。本兼容入口将仓库
    根目录加入 ``group5`` 包的子模块搜索路径，使两种目录布局都能使用
    相同的 ``group5.audit``、``group5.coordinator`` 等公开导入路径。

边界说明：
    本文件只处理 Python 包搜索路径，不复制源码、不修改外部模块，也不
    改变任何业务逻辑。最终标准集成布局仍应使用 ``agent-OS/group5``。
"""

from pathlib import Path


# 当前文件位于 ``仓库根/group5/__init__.py``，父目录的父目录即源码根。
_SOURCE_ROOT = str(Path(__file__).resolve().parent.parent)

# ``__path__`` 决定 Python 在何处查找 ``group5`` 的子模块。加入源码根后，
# 独立仓库布局下也能解析根目录中的 audit、contracts、coordinator 等包。
if _SOURCE_ROOT not in __path__:
    __path__.append(_SOURCE_ROOT)


__version__ = "1.0.0"
__author__ = "Group 5"
__all__ = []
