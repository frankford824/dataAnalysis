# 部署配置指南

## 部署边界

生产环境采用“单客户、单实例”的 Linux 服务器或虚拟机。业务服务、处理引擎、对象存储和 Superset 位于客户内网；Windows 和 macOS 仅作为客户端。即使一个实例当前只服务一个客户，正式记录仍保留 `enterprise_id`，API 和认证视图继续执行企业与店铺隔离。

建议最低软件版本：Docker Engine 26、Docker Compose v2.27。容量选择见 [容量规划](capacity.md)。

## 首次安装

```bash
./scripts/generate-env.sh > .env
chmod 600 .env

# 按客户环境修改域名、端口和密钥后校验
./scripts/validate-config.sh .env
docker compose config --quiet

docker compose up -d --build
./scripts/smoke-test.sh
```

常规安装不得自动写入示例公司、店铺或经营数据。首次打开业务门户后，由首位管理员在浏览器中完成：

1. 创建管理员密码；
2. 填写公司、平台账号和首家店铺；
3. 选择数据启用日期；
4. 启用内置标准经营模型。

完成后再创建受限业务账号并分配店铺。首个管理员初始化完成后，公开初始化接口会被关闭。

默认本机入口：

- 业务门户：`http://127.0.0.1:3000`
- API 文档：`http://127.0.0.1:8000/docs`
- Superset：`http://127.0.0.1:8088`
- MinIO 管理端：`http://127.0.0.1:9001`

Compose 默认把管理端口绑定到回环地址。生产环境只应通过客户管理的 TLS 反向代理开放业务门户；数据库、Redis、MinIO 和 Superset 管理端不得直接暴露到互联网。

## 身份认证

默认认证方式为平台本地账号密码：

- 密码使用 Argon2 哈希保存；
- 浏览器使用 HttpOnly、SameSite 会话 Cookie；
- 会话短期有效，可登出或由服务端撤销；
- 企业、角色和店铺范围从数据库账号加载，不从浏览器身份头加载。

生产环境通过 HTTPS 访问时必须设置：

```dotenv
SESSION_COOKIE_SECURE=true
```

系统保留可选的可信反向代理身份模式，但默认关闭。启用前必须同时配置代理来源地址和至少 32 字符的共享签名密钥；代理必须删除客户端同名请求头并使用短时签名断言重新注入。仅设置 `X-Enterprise-ID`、`X-User-ID` 或 `X-Role` 不会获得权限。

## 网络与浏览器地址

`.env` 中至少应确认：

```dotenv
WEB_BIND_ADDRESS=127.0.0.1
MANAGEMENT_BIND_ADDRESS=127.0.0.1
PUBLIC_API_BASE_URL=/api/v1
PUBLIC_SUPERSET_URL=https://bi.example.internal
SUPERSET_EMBED_ALLOWED_DOMAINS=https://analytics.example.internal
```

`PUBLIC_SUPERSET_URL` 是用户浏览器可访问的地址，不是 Docker 内部服务名。修改前端构建变量后需要重新构建：

```bash
docker compose build web
docker compose up -d web
```

反向代理需要保留 `Host` 和 `X-Forwarded-Proto`，限制请求体大小不低于平台配置的上传上限，并为普通 API 设置合理超时。不要向后端转发外部客户端自行提供的身份头。

## 文件与数据边界

- 浏览器和桌面端均受服务端 `COMMERCE_MAX_UPLOAD_BYTES` 限制；当前实现不是 10GB 流式合并方案。
- ZIP 同时限制解压总大小、压缩比和路径穿越。
- 原文件以内容哈希保存，不允许覆盖；同一企业重复上传相同内容不会重复发布。
- 内容日期必须不早于企业、店铺和数据来源三者中最晚的启用日期。
- 历史回填是独立任务，不得覆盖同一来源、同一店铺的锁定账期。

Docker 命名卷：

- `postgres-data`：账号、权限、配置、审计、认证经营数据和 Superset 元数据；
- `minio-data`：原文件、中间结果和导出；
- `redis-data`：任务队列状态；
- `superset-home`：Superset 运行文件。

## Superset

Superset 使用只读 `analytics_reader` 连接认证视图，不能读取原始表或写入业务库。普通用户从平台取得带企业和授权店铺条件的短时 guest token；实施人员使用独立管理账号进入设计环境。

门户看板与 Superset 必须读取同一认证聚合结果。若 Superset 尚未配置，管理端显示“尚未启用”，普通门户仍可使用内置经营看板，不得显示假看板。

## AI 模式

默认 `AI_MODE=disabled`，常规启动不会启动 LiteLLM。固定经营问题、金额计算、质量门禁、发布、导出和看板均不依赖 AI。

启用经过客户批准的模型后才运行：

```bash
docker compose --profile ai up -d litellm
```

外部模型只能辅助理解问题或解释结果；正式数字仍由认证查询产生。模型密钥只放在客户密钥系统或未提交的部署配置中。

## 演示环境

以下命令仅用于可丢弃演示实例：

```bash
./scripts/demo-reset.sh --confirm
```

它会删除当前 Compose 项目的命名卷，重建服务并写入明确标识的演示账号与固定对账数据。不得在已有客户数据的实例执行。人工基准位于 `examples/demo/expected-reconciliation.csv`。

## 备份、恢复与上线检查

```bash
./scripts/backup.sh /安全的备份目录
./scripts/restore.sh --confirm /安全的备份目录
./scripts/diagnose.sh
./scripts/smoke-test.sh
```

恢复属于覆盖操作，必须在隔离实例定期演练。上线前至少验证：

- 未登录访问业务 API 返回 401，伪造身份头不能提权；
- 两个企业和受限店铺账号不能越权；
- 固定样例销售、退款、费用、成本和利润与人工基准一致；
- 重复文件不增加认证行；
- CSV 与 XLSX 导出内容服从当前月份、平台和店铺；
- AI 关闭时黄金路径正常；
- PostgreSQL、Redis、MinIO 和 Superset 管理端不对外网开放；
- 备份恢复后账号、配置、文件、模型和看板引用完整。

更多资料：[安全边界](security.md)、[运维指南](operations.md)、[离线部署](offline-deployment.md)、[Superset 集成](superset.md)。
