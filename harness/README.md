# 电商财务对账 Harness

本目录是新的电商财务对账限界上下文。旧 `backend/` 和 `apps/web/` 不作为本阶段
正确性验收依据；在黄金基线和封存盲测通过前，它们只保留作迁移参考。

首期目标是：

> 不依赖 Excel/PBIX 运行时，确定性复算一个设计合作店铺的历史账期，并把每个结果
> 追溯到源文件行、规则版本、输入修订和人工裁决。

目标尚未全部完成。当前已完成真实输入冻结、有限模板画像、标准化产物、账期清单、
四条批准规则、证据驱动输入裁决、订单—平台钱包诊断核对、历史差异候选、规则学习
工作台和无模型模式；真实差额处置、黄金基线和 2605 盲测仍未完成。

## 不可破坏的边界

- 金额从源文本直接进入 `Decimal`，禁止 `Decimal(float)`。
- 核对合同、账期版本、差额处置和证据链是一等对象；损益表只是下游视图。
- 默认以支付宝/微信作为平台钱包证据与订单核对；同一钱包记录不得同时冒充银行资金。
- 只有存在独立银行流水和稳定批次桥接规则时，才启用银行三方模式。
- finance-win 由 OS 级只读账号保护；客户数据与模型调用日志不进入 Git。
- LLM 缺失时全部确定性流程可运行；LLM 只产生候选，不能修改账本。
- 终结账期不能被普通重跑覆盖；迟到文件只能形成有证据的调整或重述。
- 有限模板未命中必须进入待处理，不得让模型开放式猜测文件类型。

## 本地开发

```bash
cd /home/wsfwk/dataAnalysis
python3 -m venv harness/.venv
harness/.venv/bin/pip install -e './host-agent' -e './harness[dev]'

harness/.venv/bin/python -m commerce_harness \
  init --workspace ~/fa-workbench
cp harness/config.example.toml ~/fa-workbench/config.toml
```

真实配置必须位于仓库外，并显式填写只读来源目录。默认工作台分层：

```text
~/fa-workbench/
  snapshots/       # 不可变、内容寻址的原文件
  normalized/      # 内容寻址 Parquet
  reports/         # 冻结报告与证据包
  llm_logs/        # 受控模型调用日志
  ledger.duckdb    # 合同、账期、规则、裁决和证据链
```

执行真实只读流程：

```bash
export PYTHONPATH=/home/wsfwk/dataAnalysis/harness:/home/wsfwk/dataAnalysis/host-agent
python -m commerce_harness --config ~/fa-workbench/config.toml \
  scan --workspace ~/fa-workbench
python -m commerce_harness --config ~/fa-workbench/config.toml \
  freeze --workspace ~/fa-workbench
python -m commerce_harness --config ~/fa-workbench/config.toml \
  profile --workspace ~/fa-workbench
python -m commerce_harness --config ~/fa-workbench/config.toml \
  normalize --workspace ~/fa-workbench
python -m commerce_harness --config ~/fa-workbench/config.toml \
  adjudicate --workspace ~/fa-workbench
python -m commerce_harness --config ~/fa-workbench/config.toml \
  recon --workspace ~/fa-workbench --period 2604
```

## 测试

```bash
cd /home/wsfwk/dataAnalysis
PYTHONPATH=harness:host-agent harness/.venv/bin/python -m pytest harness/tests \
  --cov=commerce_harness --cov-branch \
  --cov-report=term-missing --cov-fail-under=85
```

Harness 与 Host Agent 测试不能合并在一次 pytest 调用中，因为两边有同名测试模块。
Docker 和前端命令见根 README 与 `docs/harness-deployment.md`。

## 阶段门禁

阶段 A 必须依次通过：

1. 输入冻结；
2. 期间清单和业务口径裁决；
3. 规则翻译、回放与批准；
4. 确定性订单—平台钱包核对与守恒校验；若启用银行模式，再验证独立资金桥；
5. 历史差异逐项裁决；
6. 黄金基线冻结；
7. 2605 封存盲测。

阶段 B 的模型建议、收件箱和报告不能绕过这些门禁。容器健康、页面可打开或文件已识别
都不等于账目已经正确。
