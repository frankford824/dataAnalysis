# finance-host-agent

`finance-host-agent` 是运行在 Docker 之外的只读采集与确定性计算代理。Docker
中的控制面只负责身份、配置、任务编排、审核与结果查询；CSV/XLSX 画像以及
Polars/DuckDB 重计算只在本包执行。

本期边界很明确：

- 通过 SSH alias `finance-win-ro` 只读扫描 Windows 文件；
- 不把凭据写进 TOML，不向 finance-win 写临时文件；
- PBIX 只登记路径、大小、修改时间、稳定标识和按需 SHA-256，不解析运行时金额；
- CSV/XLSX 只使用有限表头别名和确定性分类，不让 LLM 直接计算金额；
- 控制面不可用时，进度事件保存在本地 SQLite，恢复后按幂等键补报；
- 采集命令不在 Docker 中运行；容器不会获得 SSH 私钥或 Windows 凭据。

## 读取范围

代码不再内置客户目录。必须在仓库外配置文件中逐项声明允许根目录、业务用途和扩展名；
没有来源配置时扫描会拒绝启动。`config.example.toml` 只含占位路径。

以下内容默认排除：工资、汇总、学习、测试、WeChat、凭证、早期数据、回收站，
以及密钥、数据库、备份、ZIP、临时文件。普通 reparse 目录不进入；由于
finance-win 的 OneDrive 会把已落盘的目录和文件也标记成 reparse，本包只对同时
带 `Pinned` 且不带离线属性的 OneDrive reparse 做窄化只读例外。文件必须稳定
600 秒；`Offline`、`Unpinned`、`RecallOnDataAccess` 文件会跳过，避免自动触发
OneDrive 下载。所有结果仍需通过允许根与扩展名白名单。

当前开发环境在 2026-07-23 已验证专用账号只能读取目标根目录。该结论只适用于当时
检查的机器和 ACL；每次客户部署都必须重新验证专用服务账号只拥有
`ReadAndExecute/List/ReadAttributes`，代码白名单不能代替 OS 权限。

## 安装

Python 3.11 或更高版本：

```bash
cd host-agent
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Windows PowerShell 激活方式：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

复制配置但不要加入凭据：

```bash
cp config.example.toml config.toml
```

SSH 必须已能无交互连接。`ssh_binary = "auto"` 在 WSL 下会优先调用 Windows
OpenSSH，从而复用 `C:\Users\...\ .ssh\config` 中的 `finance-win-ro` alias 和私钥；
原生 Linux/Windows 使用 PATH 中的 `ssh`：

```bash
ssh -o BatchMode=yes finance-win-ro hostname
```

## CLI

直接只读扫描，不联系控制面：

```bash
python -m finance_agent --config config.toml scan
```

需要建立 PBIX 内容版本时显式读取稳定文件并计算哈希：

```bash
python -m finance_agent --config config.toml scan --hash-pbix
```

生成单个本地 CSV/XLSX 的结构画像：

```bash
python -m finance_agent profile ./fixtures/orders.csv --purpose orders
```

不联系控制面，直接验证确定性重计算：

```bash
python -m finance_agent recompute ./fixtures/orders.csv \
  --business-key 订单号 \
  --amount-column 销售额 \
  --amount-column 退款金额 \
  --output /tmp/orders-normalized.parquet
```

首次登记。注册令牌只通过环境变量注入：

```bash
export FINANCE_AGENT_ENROLLMENT_TOKEN='一次性注册令牌'
python -m finance_agent --config config.toml register
```

领取并执行至多一个任务：

```bash
python -m finance_agent --config config.toml once
```

持续运行：

```bash
python -m finance_agent --config config.toml daemon
```

开发机可将 `config.fixture.example.toml` 中的 `fixture_root` 指向一组无敏感信息
的 CSV/XLSX。该模式使用相同的安全、画像、幂等和任务流程，不需要 finance-win。

## 控制面协议

代理使用以下 HTTP 合同：

- `POST /api/v1/agents/register`
- `POST /api/v1/agents/heartbeat`
- `POST /api/v1/agent-jobs/claim`
- `POST /api/v1/agent-jobs/{id}/progress`
- `POST /api/v1/agent-jobs/{id}/complete`
- `POST /api/v1/agent-jobs/{id}/fail`

控制面发下来的任务类型：

- `scan`：返回允许范围内的文件清单与清单 checksum；任务带
  `{"hash_pbix": true}` 时为 PBIX 计算稳定 SHA-256；
- `profile`：只读流式取得指定文件，返回表头、类型、样例和结构指纹；
- `recompute`：按明确的业务键和金额列执行 Decimal(20,4) 规范化、去重与汇总，
  输出 Parquet 和确定性 checksum。

每条进度和终态都有稳定幂等键。已完成任务按 `idempotency_key` 缓存结果，控制面
重复投递时不会再次计算。

## 安全与真实性限制

- SSH 流式读取前后会核对路径、属性和文件大小；远端命令使用 Base64 编码脚本，
  文件路径不拼进 PowerShell 表达式。
- 远端文件通过 SSH 标准输出写到代理私有工作目录，不在 finance-win 创建文件，
  也不在内存中拼接大文件。
- `.xls` 可以被发现，但自动画像明确拒绝；需另存为 `.xlsx` 或 CSV。
- PBIX 可以按任务计算稳定 SHA-256，但默认扫描不读取完整 PBIX；现阶段不存在
  PBIX 模型或金额自动解析。
- 结构画像中的样例值只返回类型、长度、月份和短哈希等脱敏摘要，不上报原始订单号、
  姓名或金额文本。
- Recent 快捷方式只能说明目标曾被打开，不能证明刷新、发布或指标修改。
- 当前包定义控制面 HTTP 合同，但后端实现不在本目录范围内；在控制面端点接通前，
  `scan` 和 `profile` 可独立验证，`once/daemon` 会诚实报连接失败或把待发送事件
  留在本地状态库。

## 测试

```bash
python -m pytest
python -m pytest --cov=finance_agent --cov-report=term-missing
```
