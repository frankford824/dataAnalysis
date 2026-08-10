# ledger

把各平台交上来的表算成账。

店长每天、每周或者任意时刻把导出的表交上来，引擎自己认出这是哪家店、哪个平台、
哪个账期、每张表是什么、该按谁的口径算，然后出一张损益表，并且在出数之前先说清
这个月能不能结账、缺什么。

规则不写在代码里，写在模型数据里。加一个平台、改一项利润口径，改的是 YAML，
不是 Python。

## 装

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 用

命令行，一个路径就够：

```bash
.venv/bin/python -m ledger.cli run /path/to/平台数据
.venv/bin/python -m ledger.cli run /path/to/平台数据 --store 淘宝喜必顺
.venv/bin/python -m ledger.cli run /path/to/平台数据 --json    # 给接口和界面用
.venv/bin/python -m ledger.cli stores                          # 看店铺注册表
```

网页版，店长用这个：

```bash
.venv/bin/python -m uvicorn ledger.api:app --port 8848
```

打开 <http://127.0.0.1:8848/>，把这个月的表拖进去。

**文件名不能改。** 认哪家店、认哪张表全靠它——交上来的文件名形如
`聚水潭成本-淘宝喜必顺.xlsx`，破折号前是类别、后面是店铺。改了名字，引擎认不出
归属，那批数据不会进账（会明确列出来，不会悄悄算漏）。

## 模型

一个模型是一个目录，`models/cn-ecommerce` 是国内电商这一套：

| 文件 | 是什么 |
| --- | --- |
| `stores.yaml` | 店铺、平台、法人主体、归档状态 |
| `sources.yaml` | 每类数据由谁交、多久交一次、结账是否必须 |
| `templates.yaml` | 表头长什么样、哪列是什么、怎么取订单号、怎么归科目 |
| `metrics.yaml` | 每个口径项怎么算、挂到哪、怎么分摊 |
| `statement.yaml` | 损益表的公式树 |
| `checks.yaml` | 结账前的拦截条件 |
| `dictionary.csv` | 平台原始科目到统一科目的映射 |

各平台的利润口径本来就不一样，模型如实表达而不是强行统一：淘宝七项费用按销售收入
占比分摊到子订单，1688 按笔数均摊且补发成本并进商品成本，抖音直接取每个子订单的
结算净额。指标上用 `by_platform` 写平台差异，对外仍是同一套损益项。

## 加一家店

往 `stores.yaml` 里加一条：

```yaml
- id: pdd_xinbao
  name: 拼多多新店
  platform: pdd
  entity: 某某有限公司
```

交上来的文件如果认不出归属，CLI 和页面都会列出来，并且照着文件名给一条可以直接
抄的建议。平台前缀能认出来的会一并填好——那只是建议，猜出来的东西不参与任何计算。

## 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                    # 全部
.venv/bin/python -m pytest -m "not slow"      # 跳过要真实数据的
```

最有价值的是 `tests/test_acceptance.py`：它拿引擎算的账和人工维护的 Excel 逐项对，
断言三家店的未解释差异为零。它需要真实的平台导出（不在仓库里），没有就自动跳过。

其余测试各钉一个在真实数据上踩过的坑，docstring 里写明是哪一个。改动这些测试前
先读那段说明——它们防的都是不会报错、只会算错的问题。
