"""Oracle MCP 服务器主入口。

本模块使用 FastMCP（mcp 库的高层 API）将 Oracle 数据库操作封装为 MCP 工具，
通过标准输入/输出（stdio）传输与 MCP 客户端通信（如 Claude Desktop、WorkBuddy 等）。

设计要点：
- 启动时自动加载 .env 配置；
- 日志输出到 stderr（绝不能写 stdout，否则会污染 stdio 上的 MCP JSON-RPC 报文）；
- 所有工具均捕获数据库异常，返回友好错误信息（而非让协议层崩溃）；
- execute_query 仅允许 SELECT（由 database 层语法白名单强制），并限制最大返回行数。
"""

import json
import logging
import sys
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import database

# ---------------------------------------------------------------------------
# 日志配置：输出到 stderr，避免干扰 stdio 上的 MCP JSON-RPC 通信
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("oracle-mcp-server")

# 启动时加载 .env 文件中的环境变量（若存在）
load_dotenv()
logger.info("已加载 .env 配置（如存在）")

# 创建 MCP 服务器实例，名称用于客户端展示
mcp = FastMCP("oracle-mcp-server")

# 查询结果最大返回行数，防止一次性拉取超大结果集
MAX_ROWS = 1000


def _error(msg: str, exc: Exception) -> str:
    """统一错误包装：记录完整堆栈到日志，返回给客户端的只含友好信息。"""
    logger.exception("%s | 原始错误: %s", msg, exc)
    return json.dumps({"error": msg}, ensure_ascii=False)


@mcp.tool()
def execute_query(sql: str, schemaName: Optional[str] = None) -> str:
    """执行只读 SQL 查询（仅允许 SELECT/WITH），结果以 JSON 数组返回。

    Args:
        sql: SELECT 语句，绑定变量使用 :name 占位（不要字符串拼接值）。
        schemaName: 指定默认 schema，未限定表名将解析到该 schema（如 "SCOTT"）。
    """
    try:
        if not sql or not sql.strip():
            return _error("SQL 不能为空", ValueError("empty sql"))
        # database 层做语法白名单校验：仅 SELECT/WITH，禁注释/多语句/危险关键字
        rows = database.execute_query(sql, limit=MAX_ROWS, schemaName=schemaName)
        logger.info("execute_query 成功，返回 %d 行（上限 %d）", len(rows), MAX_ROWS)
        return json.dumps(rows, ensure_ascii=False, default=str)
    except PermissionError as exc:
        return _error("权限不足或当前为只读模式，禁止该操作", exc)
    except ValueError as exc:
        return _error(f"SQL 校验失败：{exc}", exc)
    except Exception as exc:
        return _error(f"查询执行失败：{exc}", exc)


@mcp.tool()
def list_tables(schema: Optional[str] = None) -> str:
    """列出数据库中的表；指定 schema 时仅列出该模式下的表。

    Args:
        schema: 目标用户名（不区分大小写）；不传则列出当前用户的表。
    """
    try:
        rows = database.list_tables(schema)
        logger.info("list_tables(schema=%s) 成功，共 %d 张表", schema, len(rows))
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as exc:
        return _error(f"列出表失败：{exc}", exc)


@mcp.tool()
def describe_table(table_name: str, schemaName: Optional[str] = None) -> str:
    """查看表结构：列名、数据类型、是否可为空。

    Args:
        table_name: 表名（不区分大小写，内部转大写匹配）。
        schemaName: 限定所属 schema（用户名）；不传则匹配所有 schema 中同名表。
    """
    try:
        if not table_name or not table_name.strip():
            return _error("表名不能为空", ValueError("empty table name"))
        rows = database.describe_table(table_name, schemaName=schemaName)
        if not rows:
            return _error(f"表不存在或无权限访问：{table_name}", LookupError(table_name))
        logger.info("describe_table(%s) 成功，共 %d 列", table_name, len(rows))
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as exc:
        return _error(f"获取表结构失败：{exc}", exc)


@mcp.tool()
def test_connection() -> str:
    """测试数据库连接是否正常，返回连接状态。"""
    try:
        result = database.test_connection()
        logger.info("test_connection 成功：%s", result)
        return json.dumps({"status": "ok", "message": "数据库连接正常"}, ensure_ascii=False)
    except Exception as exc:
        return _error(f"数据库连接失败：{exc}", exc)


def main() -> None:
    """服务器入口：以 stdio 方式运行 MCP 服务器。"""
    logger.info("Oracle MCP 服务器启动（stdio 模式）")
    mcp.run()


if __name__ == "__main__":
    main()
