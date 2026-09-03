# Oracle MCP Server

一个用于连接 Oracle 数据库的 MCP（Model Context Protocol）服务器。它将常用数据库操作封装为 MCP 工具，供 Claude Desktop、WorkBuddy 等 MCP 客户端调用。

## 功能

| 工具 | 说明 |
| --- | --- |
| `execute_query` | 执行 SELECT 查询（仅 SELECT/WITH），返回 JSON，最多 1000 行；支持 `schemaName` 指定默认 schema |
| `list_tables` | 列出指定 schema 下的表名（不传则列出当前用户的表） |
| `describe_table` | 查看表结构（列名、类型、是否可空）；支持 `schemaName` 限定所属 schema |
| `test_connection` | 测试数据库连接是否正常 |

安全机制：

- 只读模式（`ORACLE_MODE=readonly`，默认）下禁止 INSERT/UPDATE/DELETE；
- SQL 语法白名单校验（限定语句类型、禁止注释与多语句）+ 绑定变量传参，双重防注入；
- 连接池复用，thin 模式默认免 Oracle 客户端。

## 环境要求

- Python 3.10+
- 可访问的 Oracle 数据库
- 默认使用 python-oracledb 的 **thin 模式**（纯 Python，无需安装 Oracle 客户端），但 thin 模式仅支持 **Oracle 12.1+**
- 若目标库为 **11g 等老版本**，必须启用 **thick 模式**：安装 Oracle Instant Client（如 11.2 / 19c，需 64 位），并设置 `ORACLE_CLIENT_LIB_DIR` 指向其目录

### thick 模式（老版本数据库必备）

```ini
# .env 或 mcp.json 的 env 中设置
ORACLE_CLIENT_LIB_DIR=E:\A_DevTool\instantclient_11_2
```

`database.py` 检测到该变量会自动调用 `oracledb.init_oracle_client` 切换到 thick 模式。
实测：Instant Client 11.2（64 位）+ python-oracledb 4.x 可正常连接 11g 库。

## 安装

```bash
# 1) 创建并激活虚拟环境（可选但推荐）
python -m venv .venv
.\.venv\Scripts\activate        # Windows
source .venv/bin/activate       # Linux / macOS

# 2) 以可编辑模式安装（会自动装好 mcp、oracledb、python-dotenv）
pip install -e .
```

或手动安装依赖：

```bash
pip install "mcp<2" oracledb python-dotenv
```

> 注意：Oracle 驱动在 PyPI 上的包名是 `oracledb`（`python-oracledb` 是产品名，PyPI 无此包名）；
> `mcp` 需锁定 `<2`（2.x 已将 FastMCP 重命名为 MCPServer，与本服务器代码不兼容）。

## 配置

方式一：`.env` 文件（推荐本地运行时使用）

```bash
cp .env.example .env
# 编辑 .env 填写真实连接信息
```

方式二：在 MCP 客户端配置（mcp.json）的 `env` 字段中直接注入，见下文。

关键环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `ORACLE_HOST` | 数据库主机 | localhost |
| `ORACLE_PORT` | 监听端口 | 1521 |
| `ORACLE_SERVICE_NAME` | 服务名 | 必填 |
| `ORACLE_USER` | 用户名 | 必填 |
| `ORACLE_PASSWORD` | 密码 | 必填 |
| `ORACLE_MODE` | readonly（禁 DML）/ readwrite | readonly |

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

要点：

- `command` 建议写 Python 解释器的**绝对路径**（如虚拟环境 `F:/A_Study/oracle-mcp-server/.venv/Scripts/python.exe`）；
- `env` 中注入数据库连接信息后，可不依赖 `.env` 文件；
- 配置完成后在 WorkBuddy 连接器管理页对该服务器点击"信任"以启用。

### Claude Desktop

编辑 `claude_desktop_config.json`，写入同上结构即可。

## 安全提示

- 只读场景保持 `ORACLE_MODE=readonly`；确需写操作时再切 `readwrite`，并配合数据库侧最小权限账号。
- `.env` 与 `mcp.json` 含数据库凭据，均已被 `.gitignore` 忽略，切勿提交到版本库。
- 所有 SQL 均使用绑定变量传参 + 语法白名单校验，禁止在 SQL 中拼接用户输入。

## 项目结构

```
oracle-mcp-server/
├── .env.example       # 环境变量模板
├── .gitignore
├── mcp.json           # WorkBuddy MCP 配置示例（勿填真实密码提交）
├── pyproject.toml     # 项目配置、依赖与启动命令入口
├── run_server.py      # 启动脚本（预校验 + 横幅 + 进入主循环）
├── server.py          # MCP 服务器主程序（工具定义）
├── database.py        # Oracle 数据库操作封装（连接池 + 查询/防护）
└── README.md          # 使用说明
```
