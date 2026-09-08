"""多数据库操作封装。

设计要点：
- Oracle 走「原生 oracledb」路径（连接池 + 游标），已针对 11g 老库实测可用，
  避免 SQLAlchemy Oracle 方言在 11g + thick 模式下触发 ORA-24315（max identifier length 探活）的问题；
- SQL Server / MySQL / PostgreSQL / SQLite 走「SQLAlchemy 2.x」统一抽象层；
- 对外暴露的接口（execute_query / list_tables / describe_table / test_connection /
  execute_dml / get_connection）签名保持一致，server.py 无需改动即可向下兼容。

连接信息从环境变量读取，支持两种方式：
  1) 直接给出完整 SQLAlchemy URL（DATABASE_URL，仅对 SQLAlchemy 类数据库生效）；
  2) 给出 DB_TYPE（oracle/sqlserver/mysql/postgres/sqlite）+ 拆分字段（DB_HOST/DB_PORT/
     DB_USER/DB_PASSWORD/DB_NAME）。
  为兼容历史配置，当 DB_TYPE 缺省且存在 ORACLE_* 变量时，自动按 Oracle 处理。

安全约束（与原版一致）：
- 只读模式（DB_MODE=readonly，默认）下禁止执行 INSERT/UPDATE/DELETE；
- 所有外部 SQL 必须经过语法白名单校验（限定语句类型、禁止多语句与注释），
  执行时优先通过绑定变量（params，:name 占位）传参，降低 SQL 注入风险。
"""

import decimal
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# 启动时加载 .env（若存在）。放在模块导入早期，确保后续读取环境变量时已经生效。
load_dotenv()

# oracledb 始终导入：Oracle 走原生路径；其它数据库不依赖它。
import oracledb


# ---------------------------------------------------------------------------
# 通用：配置读取 + SQL 注入防护（两种路径共用）
# ---------------------------------------------------------------------------

def _get_mode() -> str:
    """读取数据库访问模式，默认 readonly。

    取值：'readonly'（默认，禁止写操作）/ 'readwrite'（允许 DML）。
    优先读取 DB_MODE，兼容旧的 ORACLE_MODE。
    """
    return os.getenv("DB_MODE", os.getenv("ORACLE_MODE", "readonly")).lower()


def _db_type() -> str:
    """读取目标数据库类型，默认 oracle。

    支持的环境变量键：DB_TYPE、DATABASE_TYPE。
    若两者均缺失，则回退到 oracle（兼容仅配置了 ORACLE_* 变量的旧场景）。
    """
    return (os.getenv("DB_TYPE") or os.getenv("DATABASE_TYPE") or "oracle").lower()


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


def _validate_identifier(name: str) -> str:
    """校验 schema / 表名标识符合法性，返回规范化后的名称。

    仅允许「首字符为字母，后续为字母/数字/_/$/#，长度 1~128」的形式，杜绝注入；
    不同数据库在引用时会按各自方言加引号包裹。
    """
    norm = name.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$", norm):
        raise ValueError(f"非法标识符: {name}")
    return norm


# ===========================================================================
# Oracle 原生路径（oracledb 连接池 + 游标，11g 实测可用）
# ===========================================================================

# 模块级 Oracle 连接池单例（首次使用时创建）
_oracle_pool: Optional[oracledb.SessionPool] = None


def _build_oracle_dsn() -> str:
    """根据环境变量构造 Oracle DSN（数据源名称）。仅用 ORACLE_SERVICE_NAME。"""
    host = os.getenv("ORACLE_HOST", "localhost")
    port = int(os.getenv("ORACLE_PORT", "1521"))
    # DB_SERVICE_NAME 优先，回退 ORACLE_SERVICE_NAME（兼容历史变量名）
    service_name = os.getenv("DB_SERVICE_NAME") or os.getenv("ORACLE_SERVICE_NAME")
    if not service_name:
        raise ValueError("未配置 Oracle 服务名（DB_SERVICE_NAME / ORACLE_SERVICE_NAME）")
    return oracledb.makedsn(host, port, service_name=service_name)


def _get_oracle_pool() -> oracledb.SessionPool:
    """懒加载并缓存 Oracle 会话连接池（单例）。

    若设置了 ORACLE_CLIENT_LIB_DIR，则启用 thick 模式（支持 11g 等老库）。
    """
    global _oracle_pool
    if _oracle_pool is None:
        user = os.getenv("DB_USER") or os.getenv("ORACLE_USER")
        password = os.getenv("DB_PASSWORD") or os.getenv("ORACLE_PASSWORD")
        if not user or not password:
            raise ValueError("ORACLE_USER / ORACLE_PASSWORD 未配置，请检查 .env")
        # 可选：启用 thick 模式（需要本地 Oracle 客户端库，11g 必须）
        client_lib = os.getenv("ORACLE_CLIENT_LIB_DIR") or os.getenv("DB_ORACLE_CLIENT_LIB_DIR")
        if client_lib:
            oracledb.init_oracle_client(lib_dir=client_lib)
        _oracle_pool = oracledb.SessionPool(
            user=user,
            password=password,
            dsn=_build_oracle_dsn(),
            min=int(os.getenv("ORACLE_POOL_MIN", "1")),
            max=int(os.getenv("ORACLE_POOL_MAX", "5")),
            increment=int(os.getenv("ORACLE_POOL_INCREMENT", "1")),
            threaded=True,
        )
    return _oracle_pool


def _get_oracle_connection() -> oracledb.Connection:
    """从 Oracle 连接池获取一个连接（使用完需 close 归还）。"""
    return _get_oracle_pool().acquire()


def _set_current_schema(cur: Any, schema: str) -> None:
    """将当前 Oracle 会话的默认 schema 设为指定 owner（仅接受合法标识符，双引号包裹防注入）。"""
    name = _validate_identifier(schema).upper()
    cur.execute(f'ALTER SESSION SET CURRENT_SCHEMA = "{name}"')


def _oracle_to_jsonable(val: Any) -> Any:
    """将 Oracle 特有类型转换为可 JSON 序列化的值（LOB→字符串，日期→ISO）。"""
    if isinstance(val, oracledb.LOB):
        return val.read()
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, (bytes, bytearray)):
        try:
            return bytes(val).decode("utf-8", "replace")
        except Exception:
            return bytes(val).hex()
    return val


def _oracle_execute_query(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    schemaName: Optional[str] = None,
) -> List[Dict[str, Any]]:
    _assert_safe_query(sql)
    conn = _get_oracle_connection()
    try:
        with conn.cursor() as cur:
            # 指定 schema 则用指定值，否则用登录用户（保持确定性，避免连接池串味）
            target = (schemaName or os.getenv("DB_USER") or os.getenv("ORACLE_USER", "")).strip()
            if target:
                _set_current_schema(cur, target)
            cur.execute(sql, params or {})
            columns = [col[0].lower() for col in cur.description]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            return [
                {columns[i]: _oracle_to_jsonable(row[i]) for i in range(len(columns))}
                for row in rows
            ]
    finally:
        conn.close()


def _oracle_execute_dml(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if _get_mode() == "readonly":
        raise PermissionError("当前为只读模式(DB_MODE=readonly)，禁止执行写操作")
    _assert_safe_dml(sql)
    conn = _get_oracle_connection()
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


def _oracle_list_tables(schema: Optional[str]) -> List[Dict[str, Any]]:
    if not schema:
        schema = os.getenv("DB_USER") or os.getenv("ORACLE_USER", "")
    sql = "SELECT table_name FROM all_tables WHERE owner = :owner ORDER BY table_name"
    return _oracle_execute_query(sql, {"owner": schema.upper()})


def _oracle_describe_table(table_name: str, schemaName: Optional[str]) -> List[Dict[str, Any]]:
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
    return _oracle_execute_query(sql, params)


def _oracle_test_connection() -> Dict[str, Any]:
    conn = _get_oracle_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM DUAL")
            cur.fetchone()
        return {"status": "ok"}
    finally:
        conn.close()


# ===========================================================================
# SQLAlchemy 路径（SQL Server / MySQL / PostgreSQL / SQLite）
# ===========================================================================

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

# 模块级 SQLAlchemy 引擎单例（首次使用时创建）
_sa_engine: Optional[Engine] = None


def _default_port(db_type: str) -> str:
    """各数据库类型的默认端口；sqlite 无端口返回空串。"""
    return {
        "sqlserver": "1433",
        "mssql": "1433",
        "mysql": "3306",
        "mariadb": "3306",
        "postgres": "5432",
        "postgresql": "5432",
        "sqlite": "",
    }.get(db_type, "1433")


def _build_sa_url() -> str:
    """构造 SQLAlchemy 连接 URL（仅对 SQL Server/MySQL/PostgreSQL/SQLite 生效）。"""
    # 情况 1：用户直接给出了完整 URL
    full = os.getenv("DATABASE_URL")
    if full:
        return full

    from urllib.parse import quote_plus

    db_type = _db_type()
    user = os.getenv("DB_USER") or ""
    password = os.getenv("DB_PASSWORD") or ""
    host = os.getenv("DB_HOST") or "localhost"
    port = os.getenv("DB_PORT") or _default_port(db_type)
    dbname = os.getenv("DB_NAME") or ""
    # 凭据/路径中的特殊字符统一做百分号编码，避免破坏 URL
    u = quote_plus(user)
    p = quote_plus(password)

    if db_type in ("sqlserver", "mssql"):
        driver = os.getenv("DB_ODBC_DRIVER", "ODBC+Driver+17+for+SQL+Server")
        return f"mssql+pyodbc://{u}:{p}@{host}:{port}/{dbname}?driver={driver}"
    if db_type in ("mysql", "mariadb"):
        return f"mysql+pymysql://{u}:{p}@{host}:{port}/{dbname}"
    if db_type in ("postgres", "postgresql"):
        return f"postgresql+psycopg2://{u}:{p}@{host}:{port}/{dbname}"
    if db_type == "sqlite":
        path = os.getenv("DB_NAME") or os.getenv("SQLITE_PATH") or ":memory:"
        return f"sqlite:///{path}"
    raise ValueError(f"不支持的 DB_TYPE: {db_type}（可选 sqlserver/mysql/postgres/sqlite）")


def _get_sa_engine() -> Engine:
    """懒加载并缓存 SQLAlchemy 引擎（单例，pool_pre_ping 探活）。"""
    global _sa_engine
    if _sa_engine is None:
        _sa_engine = create_engine(_build_sa_url(), pool_pre_ping=True, future=True)
    return _sa_engine


def _sa_set_schema(conn: Any, schema: str) -> None:
    """将当前连接默认命名空间切到指定 schema（按方言加引号防注入）。"""
    name = _validate_identifier(schema)
    db_type = _db_type()
    if db_type in ("postgres", "postgresql"):
        conn.execute(text(f'SET search_path TO "{name}"'))
    elif db_type in ("mysql", "mariadb"):
        conn.execute(text(f"USE `{name}`"))
    elif db_type in ("sqlserver", "mssql"):
        conn.execute(text(f"USE [{name}]"))
    else:
        raise ValueError(f"当前数据库类型({db_type})不支持指定 schema")


def _sa_to_jsonable(val: Any) -> Any:
    """将 SQLAlchemy 结果中的特殊类型转为可 JSON 序列化值。"""
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        try:
            return bytes(val).decode("utf-8", "replace")
        except Exception:
            return bytes(val).hex()
    return val


def _sa_execute_query(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    schemaName: Optional[str] = None,
) -> List[Dict[str, Any]]:
    _assert_safe_query(sql)
    engine = _get_sa_engine()
    with engine.connect() as conn:
        if schemaName:
            _sa_set_schema(conn, schemaName)
        result = conn.execute(text(sql), params or {})
        columns = [str(c).lower() for c in result.keys()]
        rows = result.fetchmany(limit) if limit else result.fetchall()
        return [
            {columns[i]: _sa_to_jsonable(row[i]) for i in range(len(columns))}
            for row in rows
        ]


def _sa_execute_dml(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if _get_mode() == "readonly":
        raise PermissionError("当前为只读模式(DB_MODE=readonly)，禁止执行写操作")
    _assert_safe_dml(sql)
    engine = _get_sa_engine()
    with engine.begin() as conn:  # engine.begin() 自动提交
        result = conn.execute(text(sql), params or {})
        return {"rowcount": result.rowcount, "status": "committed"}


def _sa_list_tables(schema: Optional[str]) -> List[Dict[str, Any]]:
    insp = inspect(_get_sa_engine())
    names = insp.get_table_names(schema=schema)
    return [{"table_name": n} for n in names]


def _sa_describe_table(table_name: str, schemaName: Optional[str]) -> List[Dict[str, Any]]:
    if not table_name or not table_name.strip():
        raise ValueError("表名不能为空")
    insp = inspect(_get_sa_engine())
    cols = insp.get_columns(table_name, schema=schemaName)
    return [
        {
            "column_name": c["name"],
            "data_type": str(c["type"]),
            "nullable": "Y" if c["nullable"] else "N",
        }
        for c in cols
    ]


def _sa_test_connection() -> Dict[str, Any]:
    engine = _get_sa_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


# ===========================================================================
# 对外统一 API：根据 DB_TYPE 分发到对应实现
# ===========================================================================

def get_connection() -> Any:
    """获取一个数据库连接（上下文管理器用法：with database.get_connection() as conn）。

    Oracle 返回 oracledb.Connection（需 close 归还连接池）；其它库返回 SQLAlchemy Connection。
    调用方使用完毕后应关闭连接；execute_query/execute_dml 内部已自动处理。
    """
    if _db_type() == "oracle":
        return _get_oracle_connection()
    return _get_sa_engine().connect()


def execute_query(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    schemaName: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """执行只读查询（SELECT/WITH），返回结果列表（每行为「列名(小写)->值」字典）。

    Args:
        sql: SELECT / WITH 语句，绑定变量使用 :name 占位。
        params: 绑定变量字典，例如 {"deptno": 10}。
        limit: 最多返回的行数；None 表示不限制（由调用方控制上限）。
        schemaName: 指定默认命名空间（Oracle=owner，PostgreSQL=schema，
                    MySQL/SQL Server=数据库名），未限定表名将解析到该命名空间。

    Raises:
        ValueError: SQL 未通过语法白名单校验，或 schemaName 非法。
    """
    if _db_type() == "oracle":
        return _oracle_execute_query(sql, params, limit, schemaName)
    return _sa_execute_query(sql, params, limit, schemaName)


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
        PermissionError: 当前为只读模式（DB_MODE=readonly）。
        ValueError: SQL 未通过语法白名单校验。
    """
    if _db_type() == "oracle":
        return _oracle_execute_dml(sql, params)
    return _sa_execute_dml(sql, params)


def list_tables(schema: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出指定命名空间（schema/数据库）下的所有表。

    Args:
        schema: 目标 schema；不传则列出当前默认命名空间下的表。

    Returns:
        形如 [{"table_name": "EMP"}, ...] 的列表。
    """
    if _db_type() == "oracle":
        return _oracle_list_tables(schema)
    return _sa_list_tables(schema)


def describe_table(
    table_name: str,
    schemaName: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """获取表结构：列名、数据类型、是否可空。

    Args:
        table_name: 表名。
        schemaName: 限定所属 schema（Oracle 为 owner，PostgreSQL 为 schema，
                    MySQL/SQL Server 为数据库名）。

    Returns:
        形如 [{"column_name": "EMPNO", "data_type": "NUMBER", "nullable": "N"}, ...]。
    """
    if _db_type() == "oracle":
        return _oracle_describe_table(table_name, schemaName)
    return _sa_describe_table(table_name, schemaName)


def test_connection() -> Dict[str, Any]:
    """测试数据库连通性。

    Returns:
        {"status": "ok"}；连接失败时抛出原始异常，由调用方包装为友好错误信息。
    """
    if _db_type() == "oracle":
        return _oracle_test_connection()
    return _sa_test_connection()
