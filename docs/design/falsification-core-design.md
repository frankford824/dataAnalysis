# 证伪内核设计:不变量合同 · 反事实实验 · 裁决语料库 · 规则包

## 这份文档定义什么

四件互相咬合的东西,它们共同构成 harness 从"电商对账工具"变成"可对外授权的领域标品"的转轴:

1. **不变量合同**:领域无关地声明"什么叫对",替代写死三边的 `reconciliation_contract`
2. **反事实实验**:把一个语义假设变成可度量、可裁决的实验,是模型输出转成资产的唯一通道
3. **裁决语料库**:唯一随模型变强而升值的资产;学习与压缩是它的同一个机制
4. **规则包**:脱敏签名后跨客户交付,让第 N 个客户的成本低于第 1 个

设计约束贯穿全文:**模型可以起草和提假设,但不参与金额算术,不写认证账本,不决定实验判定。判定权归确定性内核。**

## 与现有实现的关系

复用,不另起一套:

- `kernel/invariants.py` 已有 `deterministic_checksum` / `assert_amount_conserved` / `assert_detail_matches_summary`。本设计新增的是它们**上面的声明层**:不变量合同编译成这些既有断言。
- `evidence_policy.evidence_binding_digest` 作为语料锚定证据的唯一方式,不新造。
- `code_identity.resolve_code_identity()` 提供实验双侧的 `code_sha`。
- `freeze._writer_lock` 单写入者约束对实验同样生效。
- 既有 `rule_definition` / `rule_version` / `rule_decision` / `adjudication` / `correction` / `residual_suggestion` / `review_decision` / `baseline` 保留,本设计扩展而非替换。
- 金额一律 `DECIMAL(38,4)`,禁止 `float` 与 `Decimal(float)`。

模块落位(包名 `commerce_harness` 暂不改,更名是独立的机械动作,不阻塞本设计):

```
commerce_harness/spec/         不变量合同 + 规则 DSL + 谓词编译器
commerce_harness/experiment/   反事实实验运行器 + 度量 + 判定
commerce_harness/corpus/       裁决语料库 + 情境指纹 + 晋升
commerce_harness/packs/        规则包打包 / 脱敏证明 / 签名 / 安装
```

---

## 一 · 不变量合同

### 为什么必须换掉三边模型

当前 `ReconciliationContract` 写死 `order / platform / cash` 三个边。发票没有三个边,库存也没有。更直接的证据是:当前实现已经因为这个模型产生死锁——平台按单扣的小额费用行(订单侧为 0、平台侧为负)被判为 `missing_side`,而 `certifiable` 要求未决为零,所以 `pnl_cell` 永远为 0。

不变量合同把"三方勾稽"降级为**不变量的一个家族**,费用行则由规则声明为合法单边,直接进损益。

### 表达边界(最重要的设计决定)

**封闭的不变量家族,封闭的谓词语法,不接受用户提供的代码或通用表达式。** 理由:必须可确定性重放、可 diff、可自动翻译成自然语言、不能执行任意逻辑。

五个家族覆盖"数字必须对得上"的绝大多数场景:

| 家族 | 语义 | 典型场景 |
| --- | --- | --- |
| `equality` | ∑A(scope) = ∑B(scope) ± 容差 | 三方勾稽、明细对汇总、控制总额 |
| `conservation` | 期初 + 流入 − 流出 = 期末 | 资金余额、库存、往来挂账 |
| `proportionality` | target = base × rate ± 容差 | 税额、佣金率、服务费率 |
| `uniqueness` | 业务键在 scope 内不得重复 | 发票号、交易号、重复入账 |
| `completeness` | 每个 X 必须存在对应 Y | 应到文件清单、引用完整性、成本覆盖 |

每条不变量声明六件事,缺一不可:

```yaml
invariant_id: <定义规范化后的 sha256,自动生成>
family: equality
scope:                          # 不变量在什么范围内成立
  period: current
  store: each                   # each | all
  currency: CNY
sides:                          # 参与两侧,家族决定元数数
  left:  { kinds: [orders],        select: <谓词>, sign: as_declared }
  right: { kinds: [alipay_ledger], select: <谓词>, sign: invert_expense }
tolerance:
  absolute: "0.0100"            # 绝对容差
  relative: "0.000000"          # 相对容差,两者取较宽
materiality:                    # 重大性,三者任一触发即为重大
  single_item: "500.00"
  category_cumulative: "5000.00"
  period_revenue_ratio: "0.001"
on_violation:
  legal_dispositions: [timing_difference, platform_fee, pending_investigation]
  blocks_certification: true    # 违反是否阻断认证
```

关键点:

- **金额方向必须声明,不得推断。** `sign` 只能取 `as_declared` / `invert_expense` / `absolute`。
- **容差是绝对与相对取较宽**,避免大额场景下绝对容差形同虚设。
- **重大性是三元组**,不是单笔阈值。100 笔各 100 元必须能触发,这是既有评审明确要求过的。
- `blocks_certification: false` 是分级信任的入口:不变量可以违反而不阻断出数,但结果必须带标注(见第五节)。

### 谓词语法(封闭)

```
predicate := leaf | all_of[predicate...] | any_of[predicate...] | none_of[predicate...]
leaf      := { field, op, value }
field     ∈ 标准化后的规范字段名(合同声明的字段集合,不允许自由字段)
op        ∈ eq | ne | in | not_in | prefix | suffix | contains
           | range | sign | is_null | not_null | matches_shape
```

限制:嵌套深度 ≤ 3,叶子节点 ≤ 32。**不提供正则表达式**——不可解释、有 ReDoS 风险、无法自动翻译成自然语言。

需要模式匹配时用 `matches_shape`,一个极小的形状语言:

```
D{19}        19 位数字
D{4}-D{2}    形如 2602-08
A{2}D{6,10}  两位字母 + 6 到 10 位数字
```

这覆盖了"提取 19 位订单号"这类真实需求,且能机械地翻译成中文说明。

### 编译目标

不变量合同编译成既有断言,不引入第二套算术:

- `equality` / `proportionality` → `assert_detail_matches_summary`
- `conservation` → `assert_amount_conserved`
- `uniqueness` → 分组计数(`assert_row_count` 语义)
- `completeness` → 反连接后要求空集

### 从 JSON blob 升级为一等公民

当前控制总额差异躺在 `run_log.metrics_json` 里(如 2602 的 `{"alipay":"501.90","wechat":"42171.23"}`),没有任何人看得见、无法被裁决、无法进语料。新增落表:

```sql
CREATE TABLE IF NOT EXISTS invariant_definition (
    invariant_id    VARCHAR PRIMARY KEY,     -- 规范化定义的 sha256
    domain          VARCHAR NOT NULL,        -- ecommerce_settlement | invoice | payroll ...
    family          VARCHAR NOT NULL,
    title           VARCHAR NOT NULL,        -- 自然语言标题,自动生成后可人工改写
    definition_json JSON    NOT NULL,
    origin          VARCHAR NOT NULL,        -- builtin | pack | model_drafted | human
    created_at      TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS invariant_version (
    invariant_version_id VARCHAR PRIMARY KEY,
    invariant_id    VARCHAR NOT NULL REFERENCES invariant_definition(invariant_id),
    semver          VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,        -- draft | active | retired
    approved_by     VARCHAR,
    approved_at     TIMESTAMPTZ,
    review_due_at   DATE,                    -- 到期强制复核,规则会腐烂
    supersedes      VARCHAR REFERENCES invariant_version(invariant_version_id),
    UNIQUE (invariant_id, semver)
);

CREATE TABLE IF NOT EXISTS invariant_evaluation (
    evaluation_id   VARCHAR PRIMARY KEY,
    run_id          VARCHAR NOT NULL REFERENCES run_log(run_id),
    invariant_version_id VARCHAR NOT NULL REFERENCES invariant_version(invariant_version_id),
    period_id       VARCHAR,
    store_id        VARCHAR,
    status          VARCHAR NOT NULL,        -- passed | violated | not_applicable | insufficient_input
    left_total      DECIMAL(38,4),
    right_total     DECIMAL(38,4),
    gap_amount      DECIMAL(38,4),
    participating_rows BIGINT,
    is_material     BOOLEAN NOT NULL DEFAULT false,
    evidence_json   JSON NOT NULL,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
```

`certifiable` 的判定改为读 `invariant_evaluation`:凡 `blocks_certification` 且 `violated` 的不变量存在即不可认证;其余违反只降级信任标注。

---

## 二 · 规则 DSL

不变量说"什么必须成立",规则说"怎么分类、归属、派生"。两者分开,不要合并。

五种动作,同样封闭:

| 动作 | 语义 | 解决的真实问题 |
| --- | --- | --- |
| `classify` | 给命中行打业务类别/科目 | 支付宝业务描述分类 |
| `route` | 声明行的参与方式(双边/合法单边/排除) | **平台费用行死锁** |
| `extract` | 从字段按形状派生新字段 | 19 位订单号提取 |
| `map` | 查表映射,只装无法自动恢复的例外 | 运费归属例外 |
| `derive` | 按 as-of 生效规则做声明式算术 | 成本版本按订单时点取值 |

`route` 正是当前死锁的解法,声明形态:

```yaml
rule_id: <sha256>
action: route
select:
  all_of:
    - { field: source_kind, op: eq,    value: alipay_ledger }
    - { field: amount,      op: sign,  value: negative }
    - { field: business_description, op: in, value: [软件服务费, 技术服务费, 佣金] }
participation: legal_single_sided     # 不进双边勾稽
posting_target: platform_fee          # 直接进损益成本项
rationale: 平台按单扣费为单边成本,订单侧本就不存在对应收款
```

规则复用既有 `rule_definition` / `rule_version` / `rule_decision`,新增字段容纳 `action` 与 `select`。**模型只能产出这种受约束的类型化产物,不能产出代码或 SQL。**

---

## 三 · 反事实实验(转轴)

### 定义

一个实验 = 冻结的基线运行 + 一个假设(规则或不变量的增删改)→ 影子运行 → 一组度量 → 机械判定。

### 硬约束

1. **影子运行绝不写认证账本。** `run_kind='counterfactual'`,独立命名空间,所有 trust / analytics / performance 查询必须排除。
2. **双侧身份全冻结**:`input_manifest_sha256`、`code_sha`、`rule_set_sha256`、`invariant_set_sha256`,基线侧与影子侧各一份。
3. **确定性**:同一假设重复执行,输出哈希必须比特级一致。不一致即实验无效,不是"结果不稳定"。
4. **已终结账期只读**:在已终结账期上做实验合法,但任何数字变化直接判 `rejected`——那意味着规则改动会改写历史结账数。
5. **判定权不给模型**。判定是纯函数。

### 度量口径

必须抗刷分。既有评审已经指出"99% 行数可能只覆盖 60% 金额""把差异全塞待查就能做到 100% 有去向",以下口径按此设计:

**规模类**

- `unresolved_count` / `unresolved_amount_abs`(前 / 后 / 差)
- `line_auto_rate` 行数自动处置率
- `amount_weighted_auto_rate` **金额加权**自动处置率
- `explained_amount_ratio` 已解释差额占比

**客观裁判类**

- `control_total_gap`:每个来源的控制总额差(前 / 后)。这是最硬的一项,因为它来自数据自带的汇总文件,不由引擎产生。
- `invariant_pass_delta`:各不变量 `violated → passed` 的净变化

**安全类(任一触发即否决)**

- `major_reversal_count`:重大金额反转数,**必须为 0**
- `newly_unresolved_count`:新引入的未决,防止把问题挪到别处
- `baseline_regression_count`:已冻结基线上的数字变化,逐处列出
- `evidence_integrity_failures`:变化无法追到证据行的条数

**业务价值类**

- `certifiable_before / after`
- `profit_completeness_before / after`(六个组件的到位情况)

### 判定函数

```
rejected     ← major_reversal_count > 0
             ∨ baseline_regression_count > 0(在已终结账期)
             ∨ evidence_integrity_failures > 0
             ∨ 重复执行输出哈希不一致
supported    ← 全部 blocks_certification 的相关不变量 gap 收敛进容差
             ∧ unresolved_amount_abs 下降
             ∧ newly_unresolved_count ≤ 阈值
             ∧ major_reversal_count = 0
inconclusive ← 其余
```

**只有 `supported` 的实验能支撑规则晋升。** 这是模型输出转成资产的唯一闸门。

### 落表

```sql
CREATE TABLE IF NOT EXISTS experiment (
    experiment_id   VARCHAR PRIMARY KEY,
    hypothesis_kind VARCHAR NOT NULL,        -- rule_add | rule_change | invariant_add | ...
    hypothesis_json JSON    NOT NULL,        -- 受约束的规则/不变量草案
    proposed_by     VARCHAR NOT NULL,        -- human:<id> | model:<slug> | policy:<id>
    baseline_run_id VARCHAR NOT NULL REFERENCES run_log(run_id),
    shadow_run_id   VARCHAR          REFERENCES run_log(run_id),
    scope_json      JSON    NOT NULL,        -- 账期 × 店铺 × 领域
    baseline_code_sha VARCHAR NOT NULL,
    shadow_code_sha   VARCHAR NOT NULL,
    baseline_input_sha256 VARCHAR NOT NULL,
    shadow_input_sha256   VARCHAR NOT NULL,
    output_sha256   VARCHAR,                 -- 影子结果哈希,用于复现校验
    verdict         VARCHAR NOT NULL DEFAULT 'pending',
    verdict_reasons JSON    NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    decided_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS experiment_metric (
    experiment_id   VARCHAR NOT NULL REFERENCES experiment(experiment_id),
    period_id       VARCHAR,
    store_id        VARCHAR,
    metric          VARCHAR NOT NULL,
    before_value    DECIMAL(38,4),
    after_value     DECIMAL(38,4),
    delta_value     DECIMAL(38,4),
    PRIMARY KEY (experiment_id, period_id, store_id, metric)
);

CREATE TABLE IF NOT EXISTS experiment_delta (
    delta_id        VARCHAR PRIMARY KEY,
    experiment_id   VARCHAR NOT NULL REFERENCES experiment(experiment_id),
    subject_kind    VARCHAR NOT NULL,        -- balance | pnl_cell | invariant | item
    subject_key     VARCHAR NOT NULL,
    before_amount   DECIMAL(38,4),
    after_amount    DECIMAL(38,4),
    is_material     BOOLEAN NOT NULL DEFAULT false,
    is_reversal     BOOLEAN NOT NULL DEFAULT false,
    evidence_binding_digest VARCHAR NOT NULL,
    evidence_json   JSON NOT NULL
);
```

`experiment_delta` 是"晋升审批界面"的数据源:用户看到的不是规则逻辑,是**在他已知答案的数据上会改变什么**。

---

## 四 · 裁决语料库

### 为什么这是唯一升值的资产

规则会被模型推断出来,解析器会被模型吃掉,prompt 毫无耐久性。但"个案 → 判断 → 依据 → 锚定证据 → **实测后果**"这样的五元组,只能靠真实业务运行累积,且模型越强能从中榨出越多。它必须**模型无关**:换任何模型,语料照用。

一条语料的价值几乎全在"实测后果"这一项。"这类交易归平台技术服务费"这句话没价值;"这个判断让 2602 未决从 33,248 降到 1,204、支付宝控制差从 501.90 收敛到 0.00、零重大反转"才是被现实检验过的知识。

### 情境指纹:跨客户可迁移的关键

要让 A 客户的经验能装到 B 客户,又不泄漏 A 的数据,语料必须以**结构特征**索引,而不是原始值。

`situation_fingerprint` 由以下特征规范化后哈希:

- 参与来源种类集合(如 `{orders, alipay_ledger}`)
- 符号型态(订单侧为零、平台侧为负)
- 金额量级桶(对数分桶,如 `1e0`,不是具体金额)
- 涉及字段的形状(`D{19}`)
- 业务描述的**词类**而非原文(如 `service_fee_term`,由领域词表映射)
- 不变量家族与违反类型
- 时间型态(同期 / 跨期 / 迟到)

**指纹里不得出现任何原始值。** 这是脱敏在源头就成立,而不是导出时补救。

### 落表

```sql
CREATE TABLE IF NOT EXISTS situation_fingerprint (
    fingerprint_id  VARCHAR PRIMARY KEY,     -- 结构特征规范化后的 sha256
    domain          VARCHAR NOT NULL,
    invariant_family VARCHAR,
    features_json   JSON    NOT NULL,        -- 已脱敏的结构特征
    occurrence_count BIGINT NOT NULL DEFAULT 0,
    distinct_periods INTEGER NOT NULL DEFAULT 0,
    distinct_stores  INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS adjudication_case (
    case_id         VARCHAR PRIMARY KEY,
    fingerprint_id  VARCHAR NOT NULL REFERENCES situation_fingerprint(fingerprint_id),
    domain          VARCHAR NOT NULL,
    subject_kind    VARCHAR NOT NULL,        -- unresolved_balance | invariant_violation | ...
    subject_key     VARCHAR NOT NULL,
    period_id       VARCHAR,
    store_id        VARCHAR,

    -- 判断
    disposition_kind VARCHAR NOT NULL,       -- one_off | rule_candidate | data_repair
                                             -- | adjustment_entry | reject
    posting_target  VARCHAR,
    rationale       VARCHAR NOT NULL,        -- 自然语言,必须脱敏
    decided_by      VARCHAR NOT NULL,        -- human:<id> | policy:<id>
    decided_role    VARCHAR NOT NULL,        -- 会计 | 财务负责人 | 实施
    model_suggested_by VARCHAR,              -- 模型只能是建议者
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,

    -- 锚定与验证
    evidence_binding_digest VARCHAR NOT NULL,
    verified_by_experiment  VARCHAR REFERENCES experiment(experiment_id),
    outcome_json    JSON,                    -- 实验度量快照,语料价值所在

    -- 晋升与治理
    promoted_to_rule_version VARCHAR REFERENCES rule_version(rule_version_id),
    review_due_at   DATE,
    export_allowed  BOOLEAN NOT NULL DEFAULT false,   -- 需显式授权才可进规则包
    consent_record_id VARCHAR,
    redaction_profile_version VARCHAR
);
```

### 学习即压缩

同一 `fingerprint_id` 上出现 N 次相同 `disposition_kind` 且跨 ≥2 个账期,系统主动发起晋升:生成规则草案 → 自动建实验 → 拿反事实度量当审批依据。

晋升成功后:**N 条个案压缩为 1 条规则 + N 个证据指针**,个案明细可归档,只保留指纹、计数与摘要。知识层因此是收敛的,不是膨胀的。自我学习与自我压缩是同一个机制的两面。

### 数据不无限膨胀

三层保留策略,核心论点是:**确定性买来了删除权。**

| 层 | 内容 | 保留策略 |
| --- | --- | --- |
| 原始层 | 不可变快照 | 内容哈希天然去重;超保留期后可只留哈希与 manifest,原件转冷存或删除 |
| 事实层 | 标准化 Parquet、item / balance | 账期终结后压实;只保留最新认证运行,历史运行归档 |
| 知识层 | 不变量、规则、语料、攻击库 | 永久,体积 MB 级 |

因为引擎确定性、输入内容寻址,账期终结后不必在线保留千万级 item 行——保留认证聚合、证据索引和输出哈希,需要时现场重导并可证明结果一致。非确定性系统必须保留一切,因为它无法重现;这里可以删。

配套必须先做的清理(当前库已有的真实问题):`reconciliation_balance` 跨 413 次运行累计,含 314 次 `item_count=0` 却记为 succeeded 的空跑。空跑应短路为 `skipped`,非最新运行的明细应归档。

---

## 五 · 分级信任(解开全零门禁)

当前 `certifiable = not any(unresolved, pending_decisions, incomplete_checklist, candidate_revisions, missing_rules, missing_sides, failed_controls)` 是全零容忍,在真实数据上永不成立,导致损益永久不可生成。

改为三档,每个数字自带标注:

| 标注 | 条件 |
| --- | --- |
| `certified` 已认证 | 所有 `blocks_certification` 不变量通过;重大误分类 0;未解释暴露占比 ≤ 阈值 |
| `partial` 口径不完整 | 非阻断不变量有违反,或成本/运费覆盖不全;数字可用但必须显示缺口 |
| `blocked` 不可用 | 阻断型不变量违反,或存在重大金额未决 |

`number_guard` 已强制正文不得出现未绑定数字,标注随槽位一同输出,报告里不允许出现无标注的数字。

---

## 六 · 规则包(跨客户交付)

### 包结构

```
pack/
  pack.json              清单:pack_id, domain, semver, publisher,
                         engine_compat{min,max}, depends_on[]
  invariants.json        不变量定义(领域无关的声明)
  rules.json             规则定义(受约束的动作 + 谓词)
  knowledge.json         泛化后的语料:指纹 + 判断 + 度量摘要,无原始值
  attacks.json           攻击库
  fixtures/              合成测试夹具,由指纹生成,不含真实行
  redaction.json         脱敏证明:profile 版本 + 扫描器结论
  provenance.json        贡献来源:不透明部署 ID + 授权记录 ID,不含数据
  pack.sig               对规范化字节的分离签名(Ed25519)+ 发布者 key id
```

### 脱敏必须机械强制

不能靠人工检查。打包时运行扫描器,任一命中即拒绝出包:

- 任何字符串在本地任一快照的标准化产物中出现过
- 任何值的形状命中敏感形状表(订单号、账号、手机号、姓名、路径)
- 任何金额精确到分且未被量级分桶
- 任何 `export_allowed = false` 的语料被引用

`redaction.json` 记录扫描器版本与结论,签名覆盖它。**这套基础设施必须现在就建**——事后无法回去向 A 客户索取"把你的经验卖给 B"的授权。

### 分层与冲突

安装后按层叠加,上层永远赢:

```
内置底座  <  平台包  <  行业包  <  企业规则
```

企业规则永远优先;回滚按版本 pin;引擎版本 × 包版本走兼容矩阵,不兼容拒绝加载。

### 攻击库

```sql
CREATE TABLE IF NOT EXISTS attack_case (
    attack_id       VARCHAR PRIMARY KEY,
    target          VARCHAR NOT NULL,        -- invariant | rule | gate | period_machine
                                             -- | evidence_chain | number_guard
    method_json     JSON    NOT NULL,        -- 声明式攻击构造,不是代码
    expected_detection VARCHAR NOT NULL,     -- 系统应当如何拦截
    severity        VARCHAR NOT NULL,
    discovered_by   VARCHAR NOT NULL,
    origin_pack     VARCHAR,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS attack_result (
    attack_id       VARCHAR NOT NULL REFERENCES attack_case(attack_id),
    run_id          VARCHAR NOT NULL REFERENCES run_log(run_id),
    outcome         VARCHAR NOT NULL,        -- detected | undetected | not_applicable
    detail_json     JSON NOT NULL,
    PRIMARY KEY (attack_id, run_id)
);
```

常驻红队攻击**规格本身**,不是复核建议:

- 反刷分:构造能过门禁但错的路径(大额错误藏在小额高正确率背后)
- 攻击业务键:部分退款是否让键分裂出幽灵差异
- 攻击账期机器:迟到文件是否会静默改写已终结账期
- 攻击容差:0.009 × 十万笔的累计暴露
- 攻击证据链:能否构造出无法追到原行的数字

每个成功攻击变成一条永久回归测试。跨部署累积后可对外陈述:"你的合同通过了来自 N 个部署的 M 种已知失败模式检验"——对采购与审计的说服力高于任何精度承诺。

---

## 七 · 自治阶梯的解锁条件

`autonomy.py` 现在 `effective_level` 硬编码返回 L0。在证伪内核建成前这个保守是对的,建成后按数据晋升:

| 档 | 权限 | 晋升条件 |
| --- | --- | --- |
| L0 | 只提建议 | 默认 |
| L1 | 自动起草规则并建实验,人批准 | 该类别精确率 ≥99.5%、跨 ≥2 账期、≥20 次复核、零重大金额错误 |
| L2 | 特定残差类别自动处置 | L1 稳定 ≥3 账期,且累计暴露、命中次数、规则影响范围三项均在上限内 |

任一账期出现重大金额错误,该类别立即降级,且写入 `correction` 错题本。模型任何时候都不得写认证账本、不得决定实验判定、不得选择报告里出现哪个数字。

---

## 八 · 第一个实验(必须用真实死锁开张)

用当前库里的真实状态开张,不用构造用例:

**假设**:`source_kind = alipay_ledger` 且金额为负、业务描述属服务费词类的行,`participation = legal_single_sided`,`posting_target = platform_fee`。

**范围**:`period_2602_store_xibishun`,基线为该账期最新一次 succeeded 的 reconcile 运行。

**当前基线值(已核实)**:

- `unresolved_count` = 33,248,其中 `missing_side` 27,771
- `unresolved_amount_abs` ≈ 542,632.44(missing_side 部分)
- `control_total_gap`:alipay 501.90,wechat 42,171.23
- `certifiable` = false
- `profit_completeness` = 0 / 6

**预期判定依据**:若 unresolved 显著下降、alipay 控制差向 0 收敛、`major_reversal_count = 0`、`newly_unresolved_count` 低于阈值,则 `supported`,规则可晋升,并同时产出第一条带实测后果的语料。

wechat 的 42,171.23 大概率是另一个独立假设(需要单独实验),不应混进同一个实验——一个实验只验一个假设,否则度量无法归因。

## 九 · 实施顺序

1. 不变量合同 + 谓词编译器,把既有 `failed_controls` 迁成 `invariant_evaluation`
2. 规则 DSL 的 `route` 动作(其余四种动作随后)
3. 反事实实验运行器 + 度量 + 判定函数(影子命名空间隔离)
4. 跑第八节的第一个实验,拿到第一条语料
5. 分级信任三档,替换全零门禁,让 2602 出第一份带标注的损益
6. 语料库 + 指纹 + 晋升审批(复用实验度量当审批界面)
7. 脱敏扫描器 + 规则包打包签名
8. 攻击库 + 常驻红队
9. 自治阶梯按数据解锁

前五步是解开当前死锁所必需的,同时就是标品底座的地基——战术与战略在此汇合。第六步之后开始产生跨客户的复利。

## 明确不做

- 模型生成代码或 SQL 进生产
- 通用表达式语言或用户自定义脚本
- 正则表达式作为谓词
- 模型参与金额算术、写认证账本、决定实验判定、挑选报告数字
- 影子运行写入认证表
- 已终结账期被规则改动影响
- 未经 `export_allowed` 与脱敏扫描的语料进入规则包
- 在第一个账期完整认证之前,把抽象层固化成跨行业配置平台
