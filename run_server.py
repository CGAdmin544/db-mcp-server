"""Oracle MCP 服务器启动脚本。

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

# 必需的环境变量清单：缺一不可启动
_REQUIRED_KEYS = (
    "ORACLE_USER",
    "ORACLE_PASSWORD",
    "ORACLE_HOST",
    "ORACLE_SERVICE_NAME",
)


def precheck() -> None:
    """启动前校验必需环境变量，缺失时打印缺失项并退出（exit code 1）。"""
    missing = [k for k in _REQUIRED_KEYS if not os.getenv(k)]
    if missing:
        print("启动失败：缺少必需的环境变量：", file=sys.stderr)
        for key in missing:
            print(f"  - {key}", file=sys.stderr)
        print(
            "\n请复制 .env.example 为 .env 并填写真实值，或在 mcp.json 的 env 字段中配置。",
            file=sys.stderr,
        )
        sys.exit(1)


def print_banner() -> None:
    """打印启动横幅与脱敏后的配置摘要（密码只显示是否已设置）。"""
    mode = os.getenv("ORACLE_MODE", "readonly")
    print("=" * 56, file=sys.stderr)
    print("  Oracle MCP Server (stdio)", file=sys.stderr)
    print(f"  目标库   : {os.getenv('ORACLE_USER')}@{os.getenv('ORACLE_HOST')}:{os.getenv('ORACLE_PORT', '1521')}/{os.getenv('ORACLE_SERVICE_NAME')}", file=sys.stderr)
    print(f"  访问模式 : {mode}" + ("  (只读，禁止 DML)" if mode == "readonly" else "  (读写，允许 DML)"), file=sys.stderr)
    print(f"  密码     : {'已设置' if os.getenv('ORACLE_PASSWORD') else '未设置!'}", file=sys.stderr)
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
