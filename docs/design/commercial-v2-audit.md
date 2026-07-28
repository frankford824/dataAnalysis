# 商用正确性界面整改审计

## 产品读法

面向没有技术背景的电商经营负责人和实施人员的内网 B2B 数据产品；可信、克制、清晰，重点是任务路径与数字可读性，不是炫技。

设计旋钮采用 `DESIGN_VARIANCE=4`、`MOTION_INTENSITY=2`、`VISUAL_DENSITY=6`：业务流程保持熟悉，动效只用于反馈，经营表格保持中高密度。方法参考 redesign-existing-projects 的 audit-first、brief inference、design-system map 和 pre-flight；taste-skill 明确不适合直接套用于 dashboard、data table 和 multi-step product UI，因此未采用营销 Hero、AIDA、GSAP、bento 或玻璃拟态。

## 保持

- React/Vite 路由和现有 Lucide 图标，避免与正确性整改无关的框架迁移。
- 普通用户首页、添加数据、经营看板、问业务四入口，以及独立管理入口。
- 三步上传、当前范围筛选、经营指标与店铺对比这些真实服务路径。

## 删除或隐藏

- 空模型 JSON CRUD、空 PBIX/AI 菜单、无行为箭头和固定假待办。
- 服务失败后的演示数据回退、假成功提示、虚假的“已交给实施人员”。
- 普通用户可见的批次、ETL、Parquet、SQL、字段类型等技术词。
- 无后端行为的切换器和装饰性卡片、徽章、渐变与图标。

## 调整

- 冷静深灰导航、白/浅灰内容面和单一青绿色强调色；8px 圆角、低阴影、统一边框。
- 每页一个主动作；设置表单为页内编辑，复杂映射和检查渐进展开。
- 所有数字使用 tabular-nums；上传核对只显示店铺、日期、订单、金额、重复与问题。
- 加载、空、错误、等待、成功和未启用分别来自真实状态，不互相冒充。
- 390px 移动端只突出普通用户入口；管理表格保留可滚动边界，不压成不可读列。
- 跳至正文、label、focus-visible、44px 触控目标和 reduced motion 纳入全局约束。

## 设计参考

- `commercial-v2-home.png`
- `commercial-v2-upload-review.png`
- `commercial-v2-dashboard.png`
- `commercial-v2-data-settings.png`
- `commercial-v2-problems.png`
- `commercial-v2-change-password.png`
- `commercial-v2-mobile-review.png`

概念图只用于层级、密度和状态参考，图中数值不进入运行时。
