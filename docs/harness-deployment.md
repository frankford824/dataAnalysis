# Harness 本机部署与运维指南

本文只描述当前电商财务对账 Harness。旧通用平台的 PostgreSQL/Superset 栈见
`docs/deployment.md`，它不是本阶段正确性验收入口。

## 1. 运行边界

当前默认部署由一个本机 Docker 容器完成 `scan → freeze → profile → normalize → reconcile`，
并提供单实例 DuckDB、FastAPI 和 React 页面。计算不再依赖人工逐条执行 CLI，也不会把任务发送到
另一台计算节点。

容器通过只读挂载取得 `finance-win-ro` 的专用 SSH 配置、私钥和 `known_hosts`，启动时只复制到
内存型 `tmpfs`；镜像、工作台、日志和前端响应都不保存私钥。容器不得获得 Windows 密码、
Docker Socket 或 finance-win 写权限。外部模型未配置或调用失败时，确定性计算照常运行。

## 2. 前置条件

- Python 3.11 或更高；
- Node.js 22；
- Docker Desktop 4.54 / Engine 29 或兼容版本；
- Docker Compose v2.40 或兼容版本；
- 仓库外工作台，例如 `/home/wsfwk/fa-workbench`；
- 工作台所有者 UID/GID 默认 1000:1000，可通过 `FA_UID`、`FA_GID` 调整。

## 3. 初始化工作台

```bash
cd /home/wsfwk/dataAnalysis
python3 -m venv harness/.venv
harness/.venv/bin/pip install -e './host-agent' -e './harness[dev]'

harness/.venv/bin/python -m commerce_harness \
  init --workspace /home/wsfwk/fa-workbench
cp harness/config.example.toml /home/wsfwk/fa-workbench/config.toml
```

随后按客户实际只读目录修改仓库外 `config.toml`。示例中的店铺和路径是占位值，
不能复制为正式配置。

### 店铺与账期发现范围

生产范围由 `[[source.roots]]` 白名单与 `source.scope` 共同限定。本机当前使用全发现模式：

```toml
[source.scope]
include_all_discovered = true
start_month = "2026-02"
through_current_month = true
```

系统每次运行都会把范围解析为 2026 年 2 月至当前月；当前月标记为“进行中”。平台、店铺和业务日期
只能来自文件内容、确定性表头模板或已确认的规则。目录名可以帮助定位来源，但不能单独作为账期证据。
无法唯一识别的平台、店铺或账期会进入“待确认”，不会并入任意默认店铺。

月份目录名不能成为店铺。系统会拒绝“2月”“十二月份”、纯日期和纯编号等候选名称；即使这些名称
出现在配置或目录层级中，也只能作为时间或路径线索，不能生成逻辑店铺。店铺必须同时具备可确认的
平台信息以及订单、账单、费用或成本等电商业务证据。

按年维护的店铺文件（例如“26年发货运费.xlsx”）只要位于已授权店铺目录，会先作为同年度候选
封存；最终每一行仍按文件内容中的业务日期进入具体月份。文件名中的“26年”只能决定候选年份，
不能替代行级日期，也不能让没有该月明细的月份产生金额。

仍可用 `shop`、`shops` 和固定 `periods` 做隔离验收，但它们不能与 `include_all_discovered` 混用。
每个识别出的“平台 + 逻辑店铺”拥有稳定身份、独立合同、独立账期和独立应到清单；同名店铺在不同平台
不会合并。自动计算逐店逐月执行，单一范围失败不会阻止其他范围，但失败范围不会进入认证结果。

目标范围刷新时，已不在当前计划中的旧合同会改为“已停用”并写入结束日期，不会物理删除。该合同的
旧账期、原始快照、标准化产物、运行记录、裁决和证据仍保留，可继续用于历史查询与审计回放；停用只
影响后续自动计算范围。

PBIX 与操作日志会被封存为证据或画像来源；PBIX 不是账单金额来源，也不会因为存在 PBIX 就虚构店铺、
月份或指标。当前仅由已支持的 CSV/XLSX/ZIP 模板产生确定性金额，其他格式明确列入待处理。

计算结果使用独立的“内核代码身份”：只包含确定性 Harness、Host Agent 和对应依赖声明。
React 页面、测试截图和交付文档的改动不会让全部店铺重算；取数、标准化、核对、绩效或规则代码
发生变化时才会使旧结果失效并触发重新计算。这样既保留可复现性，也避免纯界面更新造成无意义的
全范围重跑。

当前正式证据规则版本是 `finite-normalization-v5`。升级后不会就地改写旧绑定：旧证据继续保留
用于审计，但模型引用、认证损益和人员绩效只接受从不可变原始快照重新生成的 v5 绑定。任何来源
缺少文件身份、真实行号，或 XLSX 缺少已冻结工作表时，该范围都会停止计算并进入待处理，而不是
默认指向第 1 行。同一聚合金额或差额的证据必须全部有效，不能只保留其中可用的几行。schema 15
启动迁移会撤销旧学习资格、移除旧绩效 current head 并将结果标为 superseded；学习策略
`autonomy-learning-v2` 会在晋升评估时重新计算完整绑定摘要，防止修复前样本继续被信任。
人员绩效执行器已升级为 `certified-person-performance-v2`；迁移同时淘汰 v1 head，页面只在当前
店铺与月份确有 v2 complete 结果时显示认证绩效可用。

默认 `[reconciliation].mode = "platform_wallet"`，只核对订单与支付宝/微信平台
钱包流水，银行流水明确不在当前范围。钱包记录只承担平台侧证据，不能重复充当独立
银行资金侧。只有客户已提供独立银行流水，并完成“平台结算批次—银行流水”桥接规则
回放后，才可切换到 `bank_three_way`。

## 4. 自动计算与诊断命令

默认 `[compute].enabled = true`。API 容器启动后会按顺序完成盘点、内容寻址封存、结构画像、
目标计划刷新、逐店逐月标准化和核对；每小时检查一次源清单。源文件、代码身份、已批准规则和范围均
未变化时会跳过重复计算。以下 CLI 仅用于实施诊断和隔离重放，不是普通运行的必需步骤：

```bash
export PYTHONPATH=/home/wsfwk/dataAnalysis/harness:/home/wsfwk/dataAnalysis/host-agent
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  scan --workspace /home/wsfwk/fa-workbench
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  freeze --workspace /home/wsfwk/fa-workbench
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  profile --workspace /home/wsfwk/fa-workbench
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  normalize --workspace /home/wsfwk/fa-workbench \
  --store <稳定店铺ID>
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  adjudicate --workspace /home/wsfwk/fa-workbench
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  recon --workspace /home/wsfwk/fa-workbench --period 2604 \
  --store <稳定店铺ID>
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  diff --workspace /home/wsfwk/fa-workbench --period 2604 \
  --store <稳定店铺ID>
python -m commerce_harness \
  --config /home/wsfwk/fa-workbench/config.toml \
  baseline --workspace /home/wsfwk/fa-workbench --period 2604 \
  --store <稳定店铺ID>
```

多店环境下手工运行必须显式传入 `--store`；这项限制用于防止同月跨店混算。自动调度器会从
目标计划中逐项传入稳定店铺 ID，不会选择“第一个店铺”。

部署前必须证明 `finance-win-ro` 对所有来源根目录只有读取、列目录和读取属性权限。
代码路径白名单和稳定窗口只是第二道防线，不能代替 OS ACL。

`normalize` 结束后会自动运行同一套幂等证据裁决。独立 `adjudicate` 命令用于重放：
只在控制总额、明细行数、公式污染、来源角色和业务内容指纹能唯一证明代表版本时自动
选择；证据不足时记录 `defer` 并停止入账，不要求操作者凭文件名猜测。

## 5. Docker 启动

### WSL 已启用 Docker 集成

```bash
export FA_WORKBENCH_BIND=//home/wsfwk/fa-workbench
export FA_CONNECTION_DIR=/home/wsfwk/fa-workbench/ssh
export FA_SSH_DIR=/home/wsfwk/.ssh
export FA_UID="$(id -u)"
export FA_GID="$(id -g)"
docker compose -f compose.harness.yaml up -d --build
```

`//home/...` 与 `/home/...` 在 Linux 上等价。这里保留双斜杠，是为了阻止部分 Docker
Desktop Compose 版本把可写 WSL 路径改写成无法挂载的 UNC 卷名。默认工作台路径就是
`//home/wsfwk/fa-workbench`，不改路径时无需显式设置。

### Docker Desktop 与 WSL 文件

先在 Docker Desktop 的 WSL Integration 中启用 Ubuntu，然后从 Ubuntu 终端执行 Compose。
不要把可写工作台改成 `\\wsl.localhost\...` 文件卷；Docker Desktop 的 Linux Engine 会把
该 UNC 字符串当成无效卷名。可用下面命令确认：

```bash
docker compose -f compose.harness.yaml config --quiet
docker compose -f compose.harness.yaml up -d --build
docker inspect finance-reconciliation-harness-workbench-1 \
  --format '{{range .Mounts}}{{println .Source .Destination}}{{end}}'
```

若 Linux 用户或工作台位置不同，只修改三个目录变量。不得把私钥复制进仓库、镜像或工作台。
容器只读挂载 SSH 目录，启动时仅将配置指定的密钥复制到 1 MiB `tmpfs`，停止容器即消失。

若镜像构建报
`docker-credential-desktop.exe: executable file not found in $PATH`，先检查：

```bash
readlink /usr/bin/docker-credential-desktop.exe
"/mnt/c/Program Files/Docker/Docker/resources/bin/docker-credential-desktop.exe" list
```

第二条能返回 JSON、第一条却指向不存在的 `/Docker/host/bin/...` 时，说明 Docker
Desktop 已挂载引擎，但 WSL 内的凭据 helper 软链接失效。可从 Windows PowerShell
以 Ubuntu 的 root 身份只修复这一个软链接：

```powershell
wsl.exe -d Ubuntu -u root -- ln -sfn `
  "/mnt/c/Program Files/Docker/Docker/resources/bin/docker-credential-desktop.exe" `
  /usr/bin/docker-credential-desktop.exe
```

随后在 Ubuntu 中执行 `docker-credential-desktop.exe list` 和
`docker pull python:3.12-slim-bookworm` 验证。该操作不读取、不复制 Docker 登录密码；
若 Windows 安装目录不同，必须先用只读搜索确认真实 helper 路径，不能照抄。

访问入口：

- 页面：<http://127.0.0.1:8765>
- 存活：<http://127.0.0.1:8765/healthz>
- 就绪：<http://127.0.0.1:8765/readyz>
- API 文档：<http://127.0.0.1:8765/api/docs>
- 自动计算：`GET /api/v1/progress`、`GET /api/v1/compute/jobs`
- 目标范围：`GET /api/v1/compute/targets`

`readyz` 会验证工作台标记、DuckDB、schema 版本和前端产物。Compose 健康检查使用
该端点，不把“进程在运行”误当作“产品可用”。真实工作台有数百万行证据且单写者正在提交时，
只读就绪查询可能短暂等待，因此 Compose 给内部请求 8 秒、容器探测 10 秒；这只是避免 2 秒
窗口造成假故障，不改变失败判定或重试次数。

2026-07-27 的全量验收覆盖 65 个平台店铺、390 个店铺月份，共 458 个任务且技术失败为 0。
其中 160 个范围等待来源，230 个范围已计算但未通过认证；当前没有认证经营结果。部署人员必须
把“任务成功”和“业务可用”分开观察，后者只能以可信度中心的完整性、金额、证据和确认门禁为准。

### 可信度中心与只读证据预览

页面“数据可信度”以“逻辑店铺 × 月份”为最小判断单元，状态包括“还差文件”“金额对不上”
“等待确认”“正在整理”和“可以使用”。状态由应到文件、最新成功核对、未核销金额、证据绑定和
必要人工确认共同决定。当前真实运行仍有缺文件和待确认范围，部署人员不得用页面可访问、任务成功、
容器健康或模型调用成功替代店铺月份门禁。

“可以使用”仅表示该店铺月份通过当前已配置的文件、金额和确认门禁，可以用于当前经营查看；
它不是外部审计意见、审计报告或法定财务结论。

待处理问题中的“打开原文件定位”通过服务端证据绑定读取内容寻址快照，可以显示：

- 原始文件名和快照标识；
- ZIP 内部成员（如适用）；
- Excel 工作表；
- 真实来源行号和目标字段；
- 目标行前后的有界上下文。

浏览器使用 Univer 只读预览 CSV/XLSX。服务端限制行窗口和最大列数，前端关闭编辑、工具栏、
公式栏和保存能力；预览不会执行 Excel 公式，也不会把修改写回快照或 finance-win。无法安全解析、
超出边界或缺少正式证据绑定时，页面必须明确失败或降级，不能猜测工作表和行号。需要离线复核时可以
下载原始快照副本，但来源目录仍保持只读。

若一个经营范围产生大量行级差额，页面先按“店铺 × 月份 × 业务原因”显示问题组和影响金额，
点击问题组后再分页展示原始记录。每条记录仍保留独立证据、决定和审计；分组不是汇总入账，也不会
让模型一次性解释或自动通过整组问题。

商品利润和人员绩效使用更严格的证据门禁：订单必须有平台商品 ID；广告商品 ID必须与当月订单商品
一致；平台费、物流费用和成本只有通过订单号唯一关联到该商品，或本身携带同一商品 ID，才可计入
商品级金额。任何未归属金额都会阻止“商品利润完整”和正式绩效发布，不允许按销售额、销量或均值
自动分摊。

## 6. 可选模型连接

页面“模型辅助”支持 OpenAI-compatible 与 Anthropic 协议，也可让服务端根据 URL 和
鉴权结果自动识别。操作顺序固定为：

1. 填入服务地址与 API Key；
2. 服务端调用该地址的 `/v1/models`，返回真实可用模型；
3. 选择模型并“应用并立即生效”，系统随即执行一次不含业务数据的真实调用；
4. 页面持续显示最近调用的成功、超时、限流或鉴权失败状态；
5. 在“待处理”中可生成业务说明草案；草案必须人工确认且永远不能写入账本；
6. 可随时停用，确定性核对不停止。

同一连接可配置提议模型和独立复核模型：

- 提议模型只生成结构化候选说明，并引用服务端提供的真实快照标识和来源行号；
- 独立复核模型只判断候选结论是否被给定证据支持，不产生正式金额；
- 两个模型标识必须不同才算独立复核；相同模型会显示“不是独立复核”；
- 任一模型失败、未配置或引用核验不通过时，候选保持人工确认状态；
- 只有正式 `evidence_binding` 才能进入模型引用核验；缺行号、缺 XLSX 工作表或仅有旧 JSON
  线索时直接降级，且不会浪费调用独立复核模型；
- 模型共识不能自动把店铺月份改为“可以使用”，也不能写账本、发布规则或修改原始文件。

页面同时显示能力清单。L0 只表示“模型不能写账本或发布规则”，并不限制证据定位、
差额草案、引用核验、修正学习等能力数量。每次建议保存候选 checksum、模型、引用核验状态；
当前真正接入外部模型的页面能力只有差额说明草案及其独立复核；资料归类、字段映射、关联建议
和规则草案仍是策略定义，尚未启用。人工解释与候选不同时追加 `correction`；独立复核失败
或未运行的建议不计为准确样本。这些样本只用于评估，在跨账期盲测真值形成前
`promotionEligible` 必须保持 `false`。

元数据写入 `/workbench/runtime/llm-provider.json`，密钥单独写入
`/workbench/secrets/`，目录权限 `0700`、文件权限 `0600`，均在仓库外。响应、状态页、
日志和浏览器都不会收到密钥。最近任务状态写入
`/workbench/runtime/llm-last-activity.json`，只含时间、用途、模型和成功/失败摘要，不保存
提示词、响应正文或密钥。模型请求只处理已脱敏上下文，不能直接修改金额、规则状态或
账期结论。错误密钥、超时、供应商限流、服务离线、配置损坏或主动停用均降级为确定性流程。

当前自动化测试以本地假服务验证两种协议，没有使用或伪造真实 OpenAI/Anthropic
凭据；客户仍需自行验证其服务商的模型权限、预算和网络策略。

模型服务不是启动依赖。未填写地址或密钥、主动停用、服务离线、鉴权失败、超时、限流、返回格式
错误，以及提议/复核模型任一不可用时，扫描、冻结、模板识别、标准化、确定性核对、可信度中心、
原始证据查看和人工待处理流程继续运行。此时页面会显示模型未启用或调用失败，不把确定性流程标为
故障，也不会生成伪造建议。

## 7. 安全限制

- 主机只监听 `127.0.0.1`；当前没有登录会话，禁止暴露到局域网。
- 容器根文件系统只读，丢弃全部 Linux capabilities，并启用
  `no-new-privileges`。
- 工作台必须可写，以追加 DuckDB 裁决、审计和内容寻址快照；已有快照不会覆盖，远端来源始终只读。
- 配置文件位于可写工作台的 `/workbench/config.toml`；业务运行不会通过 API 修改它。
- SSH 连接配置目录和宿主 SSH 目录分别只读挂载到 `/run/connection`、`/run/ssh-host`，
  随后只复制指定文件进容器内存文件系统。
- DuckDB 只允许一个服务实例、一个 Uvicorn worker；不要水平扩容。
- `FA_SOURCE_READONLY_ENFORCED=1` 是状态证明字段，真正边界仍是 finance-win ACL。

## 8. 停止与诊断

```bash
docker compose -f compose.harness.yaml ps
docker compose -f compose.harness.yaml logs --tail=200 workbench
curl -fsS http://127.0.0.1:8765/readyz
docker compose -f compose.harness.yaml down
```

停止容器不会删除 `~/fa-workbench`。不得使用会删除工作台目录的清理命令。

## 9. 备份与恢复

当前正式备份恢复演练尚未完成。人工备份前必须停止工作台以避免复制写入中的 DuckDB：

1. `docker compose -f compose.harness.yaml down`；
2. 对整个工作台做文件系统快照或离线副本；
3. 校验快照数量、`ledger.duckdb` 和 `.fa-workbench.json`；
4. 在新的隔离目录恢复后运行 `schema` 与 `/readyz`；
5. 比较 manifest、规则版本、裁决事件和报告哈希。

在自动备份、恢复演练和升级回滚门禁完成前，本版本只能用于受控本机验证。

## 10. 当前未完成

- 当前只对已支持且能唯一匹配的电商 CSV/XLSX/ZIP 模板执行金额计算；未识别文件会进入待处理，
  不会通过 LLM 猜测后直接入账。
- 可信度中心已经按店铺月份显示真实门禁，但当前运行仍有缺文件、金额差额和待确认范围；目前不能
  宣称所有店铺或月份“可以使用”。
- 当前默认使用订单—支付宝/微信平台钱包模式，银行流水明确为“不适用”；银行三方模式未启用。
- 自动范围为 2026-02 至当前月，但“已发现目标”不等于“已认证”。缺少应到文件、控制差额未归零、
  业务键无法对应或证据不足的店铺月份仍会被门禁阻断。
- PBIX 仅作为只读证据资产和人工口径参考；不运行 PBIX、不自动转换 PBIP，也不把其中展示值当成
  认证账本。
- 外部模型只生成脱敏业务解释草案，不能决定金额、修改规则或绕过门禁；无模型时核心流程不受影响。
- 员工主表、运营链接和 2026 历史绩效 CSV 只进入绩效参考层。运营链接当前可验证的月度归属
  只覆盖 2026-02 至 2026-05；后续月份列为空时不得沿用上一月。历史绩效公式用于回归对照，
  不等于认证绩效结果。参考事实从 2026-02 开始；店铺汇总表没有人员和商品粒度时只保留证据，
  不写人员绩效事实；平台不适用的费用列按零值参与原公式复核。
- 历史绩效导入产生的归属、`provisional` 人员或商品均不能进入正式绩效执行器；只有明确批准、
  状态为 active 且带冻结行证据的归属才有资格参与计算。
- 原始证据查看器可定位文件、压缩包成员、工作表和行号。新结果冻结正式绑定；旧结果从既有
  JSON 恢复出的文件和行只显示为“历史线索”，不会显示精准表格定位，也不能据此自动确认；
  无法恢复时明确显示不可用。
- 历史输出尚不足以冻结为黄金基线，全部平台和店铺仍需真实样本回放与盲测后才能声称“已对平”。
- 未启用 AI 写金额或账本、自动 PBIX 转换、任意规则执行、任意模型自治、外部审计黄金基线和
  正式人员—店铺—商品绩效。提议模型与独立复核模型都只能提供需人工确认的建议。
- 尚未补局域网身份认证、CSRF 和角色审计。
- 当前“通过系统检查”不是人工批准记录；正式发布/锁定动作仍未启用，页面不得称为“已确认”。

因此容器健康只证明工作台可运行，不证明账目已经正确或达到商用交付标准。
