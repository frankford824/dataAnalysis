# Commerce Analytics Platform

面向电商企业内网部署的数据分析产品。它从企业设定的启用日期开始接收 Excel、CSV 和 ZIP 文件，用确定性规则完成识别、去重、关联、分摊、校验和汇总，经业务确认与质量门禁后发布认证指标和看板。AI 是可关闭的辅助能力，不能写入正式金额或绕过发布门禁。

## Product surfaces

- **业务门户**：选择企业/平台/店铺和时间，三步完成文件提交、业务确认和结果查看；查看认证看板、自然语言查数与授权导出。
- **管理端**：维护组织、数据源、版本化模型/指标、PBIX 资产、AI 路由、审核发布、审计与备份状态；实施人员可进入 Superset 高级报表设计。
- **桌面客户端**：Windows/macOS 共用 React + Tauri v2；目录监控和上传都调用同一服务端处理。Windows 可用已安装的 Power BI Desktop 打开 PBIX，macOS 明确不支持该桌面能力。

## Architecture

- FastAPI/OpenAPI and PostgreSQL migrations
- Redis + Celery workers/scheduler
- MinIO S3-compatible immutable/versioned source objects
- Polars + DuckDB deterministic processing
- React/Vite web portal and Tauri desktop shell
- Apache Superset connected only to read-only `certified` views
- optional LiteLLM gateway (`AI_MODE=disabled` by default)

Every formal object is enterprise-scoped. Source definitions may bind to stores, platform accounts, business entities, enterprises, or several stores. Model assets such as PBIX are enterprise assets with many-to-many scope bindings rather than store children.

## Quick start

Docker Engine 26+ and Compose v2.27+ are recommended.

```bash
cp .env.example .env
make validate
make up
make seed
make smoke
```

Open the portal at <http://localhost:3000>, API docs at <http://localhost:8000/docs>, Superset at <http://localhost:8088>, and MinIO administration at <http://localhost:9001>. Values in `.env.example` are development-only; production configuration validation rejects them.

LiteLLM is not started by the normal command. After installing a customer-approved private model/provider configuration, use `docker compose --profile ai up -d litellm`. With no AI service or key, all deterministic processing and dashboards remain available.

## Development verification

```bash
# backend unit/integration tests and migrations
cd backend && pytest
alembic upgrade head

# web tests, type check, and production build
cd apps/web && npm ci
npm test -- --run
npm run build

# infrastructure validation and running-stack smoke test
docker compose config --quiet
./scripts/smoke-test.sh
```

## Delivery documentation

- [Deployment configuration](docs/deployment.md)
- [Capacity profiles](docs/capacity.md)
- [Operations, backup, upgrade and rollback](docs/operations.md)
- [Offline deployment](docs/offline-deployment.md)
- [Security and integration boundaries](docs/security.md)
- [Superset integration](docs/superset.md)

Use `./scripts/backup.sh`, `./scripts/restore.sh --confirm <backup>`, `./scripts/diagnose.sh`, and `./scripts/generate-sbom.sh` for operational delivery. No customer file, PBIX, API key, password, or generated backup belongs in this repository.

## License

The original platform code is licensed under Apache License 2.0; see [LICENSE](LICENSE). Bundled services remain under their respective licenses and are not copied into the closed-core source. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and generate the release-specific SPDX inventory with `make sbom`.
