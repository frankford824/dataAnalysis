# 历史方案说明：对账控制面与外置执行器

本文原先描述 PostgreSQL、Redis、MinIO 和通用控制面的方案，已经被当前垂直 Harness
重构取代，不再作为实现或验收依据。

当前有效入口：

- 产品与本机验证：`README.md`
- 对账 Harness 边界：`docs/harness-refactor.md`
- Docker 部署：`docs/harness-deployment.md`
- 确定性代码：`harness/`
- finance-win 只读连接器：`host-agent/`

## 仍然有效的原则

1. finance-win 必须使用专用 OS 级只读账号；代码路径白名单只是第二道防线。
2. PBIX 只作迁移规格，不作金额运行时。
3. 金额、分摊、关联和汇总只由精确小数确定性代码执行。
4. LLM 只能提出带证据引用的候选，不能写金额或发布规则。
5. 账期关闭后不能被普通重跑覆盖；迟到数据必须形成调整或重述。
6. 客户目录、账号、快照、DuckDB、Parquet 和模型日志不得进入 Git。

## 已废止的假设

- PostgreSQL 是当前 Harness 的唯一事实源；
- Redis/MinIO/Superset 是本阶段完成条件；
- 仓库代码内置某个客户的 Windows 目录；
- 局域网用户可以直接访问当前工作台；
- 页面可打开或容器健康即可证明账目正确。

当前 Harness 使用仓库外单实例 DuckDB 工作台。生产对账编排、黄金基线、2605 封存
盲测、备份恢复演练和局域网认证仍未完成，不能引用历史方案声称已经交付。
