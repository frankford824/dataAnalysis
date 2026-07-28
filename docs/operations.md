# 运维、备份、升级与回滚

## 日常检查

运行 `./scripts/diagnose.sh`，并监控 API 任务失败、队列深度、PostgreSQL 磁盘与连接、MinIO 容量、证书到期时间和最近一次成功备份。`/health` 只表示进程存活，`/ready` 检查数据库和对象存储，`/api/v1/health/diagnostics` 是需要登录的依赖诊断。

## 备份与恢复

`./scripts/backup.sh /安全的备份目录` 会保存应用库、Superset 元数据库和可选 LiteLLM 元数据库的自定义格式转储，三个产品桶中的当前对象，Superset/运行配置，以及 SHA-256 清单。原文件使用内容寻址且从不覆盖，因此正式原文件都会进入备份；但 MinIO 内部版本号不在恢复承诺内。Redis 只保存可丢弃的队列和缓存状态，不进入备份。备份介质属于客户数据，必须加密并限制访问。

恢复会覆盖当前数据，必须显式确认：

```bash
./scripts/restore.sh --confirm /安全的备份目录
```

脚本先校验哈希，再暂停写入服务，恢复 PostgreSQL 和对象，重启服务并执行冒烟检查。`./scripts/backup-restore-test.sh --confirm` 还会在破坏性演练前后执行双店铺确定性对账。恢复完成不能只看服务健康；还应核对配置、规则、模型、看板数量，抽查锁定月份和原文件哈希。

## 升级

1. 导出配置，记录当前 Git 提交和镜像摘要，并执行新备份。
2. 在恢复副本上演练迁移、冒烟和端到端测试。
3. 获取已签名版本，审查 `.env.example` 变化，再构建镜像。
4. 停止 worker 和 scheduler，运行数据库迁移，然后启动完整栈。
5. 验证健康、租户隔离、认证查询、导出和嵌入看板后再开放上传。

```bash
docker compose stop worker scheduler
docker compose run --rm backend /app/infra/docker/backend-entrypoint.sh migrate
docker compose up -d --build
./scripts/smoke-test.sh
```

## 回滚

数据库迁移向后兼容时，可以重新部署记录过的旧镜像摘要。若迁移不兼容，先停止所有写入服务，恢复升级前备份，再部署旧镜像。禁止让旧代码连接其版本不支持的新 schema。回滚不得重新发布或重算锁定月份；业务数据更正必须走管理员授权和审计流程。
