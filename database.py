"""Oracle 数据库操作封装。

基于 python-oracledb 驱动，从环境变量读取连接信息，并使用连接池复用连接。
安全约束：
- ORACLE_MODE=readonly（默认）时禁止执行任何 INSERT/UPDATE/DELETE；
- 所有外部 SQL 必须经过语法白名单校验（限定语句类型、禁止多语句与注释），
  执行时优先通过绑定变量（params）传参，降低 SQL 注入风险。
"""

import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import oracledb


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def _get_mode() -> str:
    """读取数据库访问模式，默认 readonly。

    取值：'readonly'（默认，禁止写操作）/ 'readwrite'（允许 DML）。
    """
    return os.getenv("ORACLE_MODE", "readonly").lower()


def _build_dsn() -> str:
    """根据环境变量构造 Oracle DSN（数据源名称）。

    仅使用 ORACLE_SERVICE_NAME（本项目要求），缺失则报错提示补充 .env。
    """
    host = os.getenv("ORACLE_HOST", "localhost")
    port = int(os.getenv("ORACLE_PORT", "1521"))
    service_name = os.getenv("ORACLE_SERVICE_NAME")
    if not service_name:
        raise ValueError("环境变量 ORACLE_SERVICE_NAME 未配置")
    return oracledb.makedsn(host, port, service_name=service_name)


# 模块级连接池单例（首次使用时创建）
_pool: Optional[oracledb.SessionPool] = None


def _get_pool() -> oracledb.SessionPool:
    """懒加载并缓存 Oracle 会话连接池（单例）。

    支持通过 ORACLE_CLIENT_LIB_DIR 切换到 thick 模式；
    其余情况使用 thin 模式（纯 Python 实现，无需 Oracle 客户端）。
    """
    global _pool
    if _pool is None:
        user = os.getenv("ORACLE_USER")
        password = os.getenv("ORACLE_PASSWORD")
        if not user or not password:
            raise ValueError("ORACLE_USER / ORACLE_PASSWORD 未配置，请检查 .env")

        # 可选：启用 thick 模式（需要本地 Oracle 客户端库）
        client_lib = os.getenv("ORACLE_CLIENT_LIB_DIR")
        if client_lib:
            oracledb.init_oracle_client(lib_dir=client_lib)

        _pool = oracledb.SessionPool(
            user=user,
            password=password,
            dsn=_build_dsn(),
            min=int(os.getenv("ORACLE_POOL_MIN", "1")),
            max=int(os.getenv("ORACLE_POOL_MAX", "5")),
            increment=int(os.getenv("ORACLE_POOL_INCREMENT", "1")),
            threaded=True,
        )
    return _pool


# ---------------------------------------------------------------------------
# SQL 注入防护：语法白名单校验
# ---------------------------------------------------------------------------

# 只读查询允许的前导关键字
_QUERY_LEAD = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)
# 写操作允许的前导关键字
_DML_LEAD = re.compile(r"^(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
# 注释与多语句特征（一律拒绝，防止注释绕过与批量注入）
_COMMENT_OR_MULTI = re.compile(r"(--|/\*|\*/|;)")
# 查询中禁止出现的数据定义/权限/控制类关键字
_QUERY_DANGER = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|"
    r"MERGE|EXEC|EXECUTE|COMMENT|RENAME)\b",
    re.IGNORECASE,
)
# 写操作中禁止夹带的数据定义/权限类关键字（INSERT/UPDATE/DELETE/MERGE 本身允许）
_DML_DANGER = re.compile(
    r"\b(DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|EXEC|EXECUTE|COMMENT|RENAME)\b",
    re.IGNORECASE,
)


def _assert_safe_query(sql: str) -> None:
    """只读查询的语法白名单校验：必须以 SELECT/WITH 开头，且不含危险关键字/注释/多语句。"""
    stripped = sql.strip()
    if not stripped:
        raise ValueError("SQL 不能为空")
    if not _QUERY_LEAD.match(stripped):
        raise ValueError("execute_query 仅允许 SELECT / WITH 查询")
    if _COMMENT_OR_MULTI.search(stripped):
        raise ValueError("SQL 中不允许出现注释(-- /* */)或多语句(;)")
    if _QUERY_DANGER.search(stripped):
        raise ValueError("SQL 含有被禁止的关键字")


def _assert_safe_dml(sql: str) -> None:
    """写操作的语法白名单校验：必须以 INSERT/UPDATE/DELETE/MERGE 开头，且不含危险结构。"""
    stripped = sql.strip()
    if not stripped:
        raise ValueError("SQL 不能为空")
    if not _DML_LEAD.match(stripped):
        raise ValueError("execute_dml 仅允许 INSERT/UPDATE/DELETE/MERGE 语句")
    if _COMMENT_OR_MULTI.search(stripped):
        raise ValueError("SQL 中不允许出现注释(-- /* */)或多语句(;)")
    if _DML_DANGER.search(stripped):
        raise ValueError("SQL 含有被禁止的关键字")


def _to_jsonable(val: Any) -> Any:
    """将 Oracle 特有类型转换为可 JSON 序列化的值（LOB→字符串/字节，日期→ISO）。"""
    if isinstance(val, oracledb.LOB):
        return val.read()
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def get_connection() -> oracledb.Connection:
    """从连接池获取一个 Oracle 数据库连接。

    调用方使用完毕后应调用 conn.close() 将连接归还连接池（execute_query/execute_dml
    内部已自动处理）。
    """
    return _get_pool().acquire()


def _set_current_schema(cur: Any, schema: str) -> None:
    """将当前会话的默认 schema 设为指定名称，使未限定表名解析到该 schema。

    仅接受合法 Oracle 标识符（字母/数字/_/$/#，首字符为字母），全程用双引号包裹，
    杜绝注入；该操作不改变数据，只读模式下亦允许。
    """
    name = schema.strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9_$#]{0,127}$", name):
        raise ValueError(f"非法 schema 名称: {schema}")
    cur.execute(f'ALTER SESSION SET CURRENT_SCHEMA = "{name}"')


def execute_query(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    schemaName: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """执行只读查询（SELECT），返回结果列表（每行为「列名(小写)->值」字典）。

    Args:
        sql: SELECT / WITH 语句，绑定变量使用 :name 占位。
        params: 绑定变量字典，例如 {"deptno": 10}。
        limit: 最多返回的行数；None 表示不限制（由调用方控制上限）。
        schemaName: 指定默认 schema，未限定表名将解析到该 schema（如 "SCOTT"）。

    Raises:
        ValueError: SQL 未通过语法白名单校验，或 schemaName 非法。
    """
    _assert_safe_query(sql)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 规范化当前 schema：指定则用指定值，否则用登录用户（保持确定性，避免连接池串味）
            target = (schemaName or os.getenv("ORACLE_USER", "")).strip()
            if target:
                _set_current_schema(cur, target)
            cur.execute(sql, params or {})
            columns = [col[0].lower() for col in cur.description]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            return [
                {columns[i]: _to_jsonable(row[i]) for i in range(len(columns))}
                for row in rows
            ]
    finally:
        conn.close()


def execute_dml(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """执行写操作（INSERT / UPDATE / DELETE / MERGE），自动提交，返回影响行数字典。

    Args:
        sql: DML 语句，绑定变量使用 :name 占位。
        params: 绑定变量字典。

    Returns:
        {"rowcount": <受影响行数>, "status": "committed"}

    Raises:
        PermissionError: 当前为只读模式（ORACLE_MODE=readonly）。
        ValueError: SQL 未通过语法白名单校验。
    """
    if _get_mode() == "readonly":
        raise PermissionError("当前为只读模式(ORACLE_MODE=readonly)，禁止执行写操作")
    _assert_safe_dml(sql)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            rowcount = cur.rowcount
            conn.commit()
            return {"rowcount": rowcount, "status": "committed"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_tables(schema: str) -> List[Dict[str, Any]]:
    """列出指定 schema（用户名）下的所有表。

    Args:
        schema: 目标用户名（不区分大小写，内部转大写匹配）。

    Returns:
        形如 [{"table_name": "EMP"}, ...] 的列表。
    """
    sql = "SELECT table_name FROM all_tables WHERE owner = :owner ORDER BY table_name"
    return execute_query(sql, {"owner": schema.upper()})


def describe_table(table_name: str, schemaName: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取表结构：列名、数据类型、是否可空。

    Args:
        table_name: 表名（不区分大小写，内部转大写匹配）。
        schemaName: 限定所属 schema（用户名）；不传则匹配所有 schema 中同名表。

    Returns:
        形如 [{"column_name": "EMPNO", "data_type": "NUMBER", "nullable": "N"}, ...]。
    """
    params: Dict[str, Any] = {"table_name": table_name.upper()}
    owner_filter = ""
    if schemaName:
        owner_filter = "AND owner = :owner"
        params["owner"] = schemaName.upper()
    sql = (
        "SELECT column_name, data_type, nullable "
        "FROM all_tab_columns "
        f"WHERE table_name = :table_name {owner_filter} "
        "ORDER BY column_id"
    )
    return execute_query(sql, params)


def test_connection() -> Dict[str, Any]:
    """测试数据库连通性：从连接池获取连接并执行 SELECT 1 FROM DUAL。

    Returns:
        {"status": "ok"}；连接失败时抛出原始异常，由调用方包装为友好错误信息。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM DUAL")
            cur.fetchone()
        return {"status": "ok"}
    finally:
        conn.close()
