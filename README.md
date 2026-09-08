# 多数据库 MCP Server

一个支持 **Oracle / SQL Server / MySQL / PostgreSQL / SQLite** 的 MCP（Model Context Protocol）服务器。底层用 SQLAlchemy 2.x 作为统一抽象层，把常用数据库操作封装为 MCP 工具，供 Claude Desktop、WorkBuddy 等 MCP 客户端调用。接口与原 Oracle 版完全兼容（`execute_query` / `list_tables` / `describe_table` / `test_connection`），原有仅配置 `ORACLE_*` 的旧环境无需改动即可继续工作。

## 功能

| 工具                | 说明                                                                     |
| ----------------- | ---------------------------------------------------------------------- |
| `execute_query`   | 执行 SELECT 查询（仅 SELECT/WITH），返回 JSON，最多 1000 行；支持 `schemaName` 指定默认命名空间 |
| `list_tables`     | 列出指定 schema/数据库下的表名（不传则列出当前默认命名空间下的表）                                  |
| `describe_table`  | 查看表结构（列名、类型、是否可空）；支持 `schemaName` 限定所属 schema                          |
| `test_connection` | 测试数据库连接是否正常                                                            |

安全机制：

- 只读模式（`DB_MODE=readonly`，默认）下禁止 INSERT/UPDATE/DELETE；
- SQL 语法白名单校验（限定语句类型、禁止注释与多语句）+ 绑定变量传参，双重防注入；
- 连接池复用（SQLAlchemy `pool_pre_ping` 探活）。

## 环境要求

- Python 3.10+
- 目标数据库可达，并安装对应驱动（见安装说明）

## 安装

```bash
# 1) 创建并激活虚拟环境（可选但推荐）
python -m venv .venv
.\.venv\Scripts\activate        # Windows
source .venv/bin/activate       # Linux / macOS

# 2) 以可编辑模式安装（会自动装好 mcp、sqlalchemy、各数据库驱动、python-dotenv）
pip install -e .

# 只装某个数据库驱动（可选）：
pip install -e ".[mysql]"        # 仅 MySQL
pip install -e ".[mssql,postgres]"  # SQL Server + PostgreSQL
```

或手动安装依赖：

```bash
pip install "mcp<2" sqlalchemy oracledb pyodbc pymysql psycopg2-binary python-dotenv
```

> 注意：`mcp` 需锁定 `<2`（2.x 已将 FastMCP 重命名为 MCPServer，与本服务器代码不兼容）。

## 配置

连接信息从环境变量读取，支持两种方式（方式一优先级最高）：

**方式 A：完整 SQLAlchemy URL**

```ini
DATABASE_URL=mysql+pymysql://user:pw@localhost:3306/mydb
```

**方式 B：拆分字段**

```ini
DB_TYPE=oracle            # oracle / sqlserver / mysql / postgres / sqlite
DB_USER=your_user
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=1521
DB_NAME=orcl_service_or_dbname
DB_MODE=readonly          # readonly（默认，禁 DML）/ readwrite
```

各 `DB_TYPE` 对应的 URL 模板（供参考）：

| DB_TYPE   | URL 模板                                                                     |
| --------- | -------------------------------------------------------------------------- |
| oracle    | `oracle+oracledb://user:pw@host:port/?service_name=XXX`                    |
| sqlserver | `mssql+pyodbc://user:pw@host:1433/db?driver=ODBC+Driver+17+for+SQL+Server` |
| mysql     | `mysql+pymysql://user:pw@host:3306/db`                                     |
| postgres  | `postgresql+psycopg2://user:pw@host:5432/db`                               |
| sqlite    | `sqlite:///C:/path/to/app.db`                                              |

**Oracle 兼容（thick 模式，仅 11g 等老库需要）**：  
thin 模式（默认）无需本地 Oracle 客户端，但仅支持 Oracle 12.1+。若目标库为 11g，必须启用 thick 模式：安装 Oracle Instant Client（如 11.2 / 19c，需 64 位），并设置 `ORACLE_CLIENT_LIB_DIR`。

```ini
ORACLE_CLIENT_LIB_DIR=E:\A_DevTool\instantclient_11_2
```

实测：Instant Client 11.2（64 位）+ python-oracledb 4.x 可正常连接 11g 库。旧环境只配 `ORACLE_*` 变量时，`DB_TYPE` 缺省会按 Oracle 处理，完全向后兼容。

## 使用

三种启动方式任选其一：

```bash
# 方式 1：pip install -e . 之后使用全局命令
oracle-mcp-server

# 方式 2：带预校验与启动横幅的启动脚本（推荐）
python run_server.py

# 方式 3：直接启动服务器主程序
python server.py
```

启动脚本 `run_server.py` 会在进入主循环前预校验必需环境变量（缺失时给出中文提示退出）并打印脱敏配置摘要；`server.py` 则直接进入 stdio 主循环。两者均可作为 MCP 客户端的启动命令。

## 接入 MCP 客户端

### WorkBuddy

编辑 `~/.workbuddy/mcp.json`（注意不是 `.mcp.json`），或参考本仓库的 `mcp.json` 示例：

```json
{
  "mcpServers": {
    "oracle": {
      "command": "python",
      "args": ["F:/A_Study/oracle-mcp-server/run_server.py"],
      "env": {
        "DB_TYPE": "oracle",
        "ORACLE_HOST": "localhost",
        "ORACLE_PORT": "1521",
        "ORACLE_SERVICE_NAME": "XEPDB1",
        "ORACLE_USER": "your_user",
        "ORACLE_PASSWORD": "your_password",
        "ORACLE_MODE": "readonly"
      }
    }
  }
}
```

其它数据库示例（替换 env 即可）：

```json
{
  "mcpServers": {
    "mysql": {
      "command": "python",
      "args": ["F:/A_Study/oracle-mcp-server/run_server.py"],
      "env": {
        "DB_TYPE": "mysql",
        "DB_HOST": "localhost",
        "DB_PORT": "3306",
        "DB_NAME": "mydb",
        "DB_USER": "your_user",
        "DB_PASSWORD": "your_password",
        "DB_MODE": "readonly"
      }
    }
  }
}
```

要点：

- `command` 建议写 Python 解释器的**绝对路径**（如虚拟环境 `F:/A_Study/oracle-mcp-server/.venv/Scripts/python.exe`），避免用到没有依赖的解释器；
- `env` 中注入数据库连接信息后，可不依赖 `.env` 文件；
- 配置完成后在 WorkBuddy 连接器管理页对该服务器点击"信任"以启用。

### Claude Desktop

编辑 `claude_desktop_config.json`，写入同上结构即可。

## 安全提示

- 只读场景保持 `DB_MODE=readonly`；确需写操作时再切 `readwrite`，并配合数据库侧最小权限账号。
- `.env` 与 `mcp.json` 含数据库凭据，均已被 `.gitignore` 忽略，切勿提交到版本库。
- 所有 SQL 均使用绑定变量传参 + 语法白名单校验，禁止在 SQL 中拼接用户输入。

## 项目结构

```
oracle-mcp-server/
├── .env.example       # 环境变量模板（多数据库）
├── .gitignore
├── mcp.json           # WorkBuddy MCP 配置示例（勿填真实密码提交）
├── pyproject.toml     # 项目配置、依赖与启动命令入口
├── run_server.py      # 启动脚本（预校验 + 横幅 + 进入主循环）
├── server.py          # MCP 服务器主程序（工具定义）
├── database.py        # 多数据库操作封装（SQLAlchemy + 查询/防护）
└── README.md          # 使用说明
```
