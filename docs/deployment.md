# 部署配置指南

当前有效产品是电商财务对账 Harness。详细操作、Windows/WSL 两种 Docker 启动方式、
安全边界、诊断和备份步骤见：

- [Harness 本机部署与运维指南](harness-deployment.md)
- [Harness 重构执行边界](harness-refactor.md)

## 当前部署形态

- finance-win：专用 OS 级只读账号，只提供原始文件读取；
- WSL 主机：执行 `scan`、`freeze`、`profile`，不向远端写文件；
- Docker：单实例 DuckDB、FastAPI 和 React 工作台；
- 客户数据：全部保存在仓库外 `~/fa-workbench`；
- 人员与商品归属：finance-win 员工表、运营链接和 2026 历史绩效文件进入只读参考层；
  历史绩效来源、临时人员和临时商品不会进入正式绩效执行器；
- 外部模型：可在页面启停，未配置或失败时不影响确定性流程。

## 快速启动

```bash
export FA_WORKBENCH_BIND=//home/wsfwk/fa-workbench
export FA_CONNECTION_DIR=/home/wsfwk/fa-workbench/ssh
export FA_SSH_DIR=/home/wsfwk/.ssh
export FA_UID="$(id -u)"
export FA_GID="$(id -g)"
export FA_EDGE_TOKEN="$(openssl rand -hex 32)"

docker compose -f compose.harness.yaml up -d --build
curl -fsS http://127.0.0.1:8765/readyz
```

页面入口为 <http://127.0.0.1:8765>。

双斜杠工作台路径用于兼容 Docker Desktop 的 WSL 路径转换；它在 Linux 中与单斜杠等价。
Docker Desktop 必须启用 Ubuntu 集成，并从 Ubuntu 终端执行 Compose。

## 重要限制

当前 API 没有多用户登录，Compose 只能绑定主机回环地址，禁止直接暴露到局域网。
容器健康只证明工作台可运行，不证明对账已经正确。以下门禁仍需完成：

1. 三项业务口径裁决；
2. 支付宝分类和订单号规则回放；
3. 订单—平台钱包确定性核对的真实样本回放；银行腿当前明确不适用；
4. 2602–2604 历史差异裁决和黄金基线；
5. 2605 封存盲测；
6. 备份恢复演练和局域网身份认证。

人员绩效页当前是历史参考核验，不是正式绩效结算。2026 年 6 月及以后没有明确负责人来源时
不会沿用 5 月归属；只有认证账本产生商品粒度结果并通过绩效政策版本校验后，才能形成正式绩效。
参考层只暴露 2026-02 之后的人员×商品明细；同目录中的店铺月汇总只作为原始证据保存。
平台模板没有使用的费用列按零值参与旧公式复核，而不是把整份文件误报为导入失败。
L0 表示模型无账本写入权限，不表示只能执行单一任务；当前外部模型只接线差额说明草案和独立复核。
当前正式证据规则为 `finite-normalization-v5`。旧版本绑定和旧 JSON 线索只保留审计，必须从
不可变快照重新处理；缺行号或 XLSX 缺工作表的证据不能通过模型引用或人员绩效门禁。独立复核失败
的样本不计入规则晋升准确率。同一金额或差额只要混入一条无效绑定，整组即不可用，不会挑选剩余
行继续计算。schema 15 会撤销旧策略学习资格和旧绩效 current head；学习 v2 每次评估都会重新
比对当前完整绑定摘要。当前绩效执行器为 `certified-person-performance-v2`，旧 v1 head 会被
撤销；“认证绩效可用”只按当前选择的店铺、月份和 complete 状态判断。
页面中的“通过系统检查”不是人工批准或正式发布记录。

上一轮 PostgreSQL、Redis、MinIO、Superset 通用平台栈保留在旧代码中作迁移参考，
不再作为当前启动入口或完成证据。
