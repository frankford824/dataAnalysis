# 商析 · 电商经营数据平台

商析是一套部署在客户内网、面向电商经营负责人的数据准备与分析产品。本版本聚焦一条可验证的标准路径：登录后建立公司和店铺，添加当月订单与费用文件，核对系统识别结果，通过确定性质量检查后发布，并在权限范围内查看、提问和导出经营结果。

正式金额不由 AI 生成。销售、退款、平台费、广告费、运费、商品成本和经营利润均由内置标准经营模型使用精确小数计算；未通过质量检查的数据不能进入认证看板。

## 当前已实现

- 本地账号密码登录、Argon2 密码哈希、短期会话、登出和会话过期。
- 企业与店铺权限隔离；受限账号只能读取被授权店铺。浏览器提交的身份或角色请求头不会被信任。
- 首次设置公司、平台账号、店铺、启用日期和标准经营模型。
- CSV、XLS/XLSX 和 ZIP 文件上传、有限模板识别、内容哈希防重和业务确认。
- 必填字段、有效日期、记录与订单数量、金额格式、重复业务键、店铺范围和可选控制总额检查。
- 发布后的销售、退款、费用、成本、利润、退款率和利润率看板。
- 当前月份、平台和授权店铺范围内的 CSV 或真实 XLSX 导出。
- 固定经营问题：本月销售、退款、费用、利润，店铺排名，上月比较，退款率和利润率。
- 实施人员待处理问题、用户权限、系统健康、配置导出和 Superset 设计入口。
- PostgreSQL、Redis、MinIO、FastAPI、Celery、React 和 Superset Docker 开发栈。

## 普通用户入口

登录后只有四个一级入口：

1. **首页**：本月数据是否完整，以及下一件需要做的事。
2. **添加本月数据**：选择范围、添加文件、核对并更新三步流程。
3. **经营看板**：认证指标、趋势、店铺对比和当前范围导出。
4. **问业务**：选择系统已支持的问题，不承诺任意 AI 问答。

管理员和实施人员通过独立的“管理”入口完成首次设置、数据设置、问题处理、用户权限、报表设计和系统诊断。

## 本地启动

建议使用 Docker Engine 26 及以上版本和 Docker Compose v2.27 及以上版本。

```bash
cp .env.example .env
./scripts/validate-config.sh .env
docker compose up -d --build
./scripts/smoke-test.sh
```

首次打开 <http://localhost:3000> 时，系统会要求创建首个管理员并完成基础经营设置。API 文档位于 <http://localhost:8000/docs>；Superset 位于 <http://localhost:8088>，只供实施人员设计高级报表。

需要重建带固定对账数据的显式演示环境时执行：

```bash
./scripts/demo-reset.sh --confirm
```

该命令会删除当前 Compose 项目的命名卷，仅能用于可丢弃的演示环境。固定人工基准见 `examples/demo/expected-reconciliation.csv`。

## 验证

```bash
cd backend
.venv/bin/pytest --cov=app
.venv/bin/alembic upgrade head

cd ../apps/web
npm ci
npm test -- --run
npm run typecheck
npm run build

cd ../..
docker compose config --quiet
./scripts/integration-e2e.sh
```

## 默认关闭或尚未启用

- 任意模型 JSON 或万能低代码建模器。
- PBIX 自动解析、PBIX 到 PBIP 无人值守转换和 macOS Power BI Desktop 支持。
- 任意自然语言问题、AI 自动写规则或 AI 直接产生正式金额。
- 浏览器超大文件上传和服务端无限大小分片合并。
- 自动目录监控作为普通用户正式入口。
- 历史数据全量回溯；历史回填只能作为独立任务，且不能覆盖锁定账期。

这些能力不会用示例数据、空页面或假成功提示冒充可用。LiteLLM 未配置或外部 AI 不可用时，确定性处理、固定经营问答和看板仍可工作。

## 交付文档

- [部署配置指南](docs/deployment.md)
- [安全边界](docs/security.md)
- [运维、备份、升级和回滚](docs/operations.md)
- [容量规划](docs/capacity.md)
- [离线部署](docs/offline-deployment.md)
- [Superset 集成](docs/superset.md)

客户文件、密码、API Key、PBIX、生成的导出和备份不得提交到仓库。

## 许可证

自研代码采用 [Apache License 2.0](LICENSE)。第三方组件继续适用各自许可证，详见 [第三方依赖声明](THIRD_PARTY_NOTICES.md) 和 `artifacts/sbom/source.spdx.json`。
