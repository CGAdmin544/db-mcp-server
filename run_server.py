"""多数据库 MCP 服务器启动脚本。

提供比 `python server.py` 更完善的启动入口：
- 打印启动横幅与当前生效配置（脱敏后）；
- 预校验必需环境变量，缺失时给出明确中文提示后退出；
- 复用 server.main() 的 stdio 运行逻辑。

用法：
    python run_server.py
或安装后使用命令：
    oracle-mcp-server
"""

import os
import sys

# 导入前加载 .env，保证预校验能看到 .env 中的配置
from dotenv import load_dotenv

load_dotenv()


def _required_keys() -> list:
    """根据连接配置计算必需的环境变量清单。

    规则：
    - 若设置了 DATABASE_URL，URL 已自包含，不强制其它变量；
    - 否则按 DB_TYPE 决定：oracle 兼容 ORACLE_* 与 DB_* 混用，其余库要求 DB_USER/PASSWORD/HOST/NAME。
    """
    if os.getenv("DATABASE_URL"):
        return []
    db_type = (os.getenv("DB_TYPE") or os.getenv("DATABASE_TYPE") or "oracle").lower()
    if db_type == "sqlite":
        return []  # sqlite 仅需 DB_NAME（文件路径）或 DATABASE_URL
    if db_type == "oracle":
        # 兼容历史：只要 ORACLE 三件套齐全即可；否则要求 DB_* 拆分字段
        if os.getenv("ORACLE_USER") and os.getenv("ORACLE_HOST") and os.getenv("ORACLE_SERVICE_NAME"):
            return ["ORACLE_PASSWORD"]
        return ["DB_USER", "DB_PASSWORD", "DB_HOST", "DB_NAME"]
    # sqlserver / mysql / postgres
    return ["DB_USER", "DB_PASSWORD", "DB_HOST", "DB_NAME"]


def precheck() -> None:
    """启动前校验必需环境变量，缺失时打印缺失项并退出（exit code 1）。"""
    missing = [k for k in _required_keys() if not os.getenv(k)]
    if missing:
        print("启动失败：缺少必需的环境变量：", file=sys.stderr)
        for key in missing:
            print(f"  - {key}", file=sys.stderr)
        print(
            "\n请复制 .env.example 为 .env 并填写真实值，或在 mcp.json 的 env 字段中配置。",
            file=sys.stderr,
        )
        sys.exit(1)


def _db_type() -> str:
    return (os.getenv("DB_TYPE") or os.getenv("DATABASE_TYPE") or "oracle").lower()


def print_banner() -> None:
    """打印启动横幅与脱敏后的配置摘要（密码只显示是否已设置）。"""
    db_type = _db_type()
    mode = os.getenv("DB_MODE", os.getenv("ORACLE_MODE", "readonly"))
    # 目标库展示：优先 DB_*，Oracle 回退到 ORACLE_*
    user = os.getenv("DB_USER") or os.getenv("ORACLE_USER") or "?"
    host = os.getenv("DB_HOST") or os.getenv("ORACLE_HOST") or "localhost"
    port = os.getenv("DB_PORT") or os.getenv("ORACLE_PORT") or ""
    name = os.getenv("DB_NAME") or os.getenv("ORACLE_SERVICE_NAME") or ""
    print("=" * 56, file=sys.stderr)
    print("  多数据库 MCP Server (stdio)", file=sys.stderr)
    print(f"  数据库类型 : {db_type}", file=sys.stderr)
    print(f"  目标库   : {user}@{host}:{port}/{name}", file=sys.stderr)
    print(f"  访问模式 : {mode}" + ("  (只读，禁止 DML)" if mode == "readonly" else "  (读写，允许 DML)"), file=sys.stderr)
    pw = os.getenv("DB_PASSWORD") or os.getenv("ORACLE_PASSWORD")
    print(f"  密码     : {'已设置' if pw else '未设置!'}", file=sys.stderr)
    print("=" * 56, file=sys.stderr)


def main() -> None:
    """入口：预校验 -> 打印横幅 -> 进入 MCP 服务器主循环。"""
    precheck()
    print_banner()
    # 延迟导入：确保日志/横幅先于服务器启动信息输出
    from server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
