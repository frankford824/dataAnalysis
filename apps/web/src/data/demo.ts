import type { DashboardSummary } from '../types'

export const enterprises = [{ id: 'demo-enterprise', name: '海风户外用品' }, { id: 'demo-enterprise-2', name: '悦味食品零售' }]
export const platforms = [{ id: '', name: '全部平台' }, { id: 'tmall', name: '天猫' }, { id: 'jd', name: '京东' }]
export const stores = [{ id: '', name: '全部店铺' }, { id: 'north', name: '北辰旗舰店' }, { id: 'south', name: '远岸商城' }, { id: 'camp', name: '山野装备店' }]

export const dashboardDemo: DashboardSummary = {
  revenue: 2418620, refund: 126480, fees: 384210, profit: 612940,
  trend: [
    ['2025-07',780,250],['2025-08',900,300],['2025-09',1040,390],['2025-10',930,315],['2025-11',1060,430],['2025-12',990,350],['2026-01',1290,535],['2026-02',930,330],['2026-03',1010,340],['2026-04',1080,350],['2026-05',1140,420],['2026-06',1310,535],
  ].map(([month,revenue,profit]) => ({ month: String(month), revenue: Number(revenue) * 1000, profit: Number(profit) * 1000 })),
  stores: [
    { id: 'north', name: '北辰旗舰店', revenue: 1126380, change: 21.7, refund: 58320, refundRate: 5.18, fees: 176540, profit: 318760, profitChange: 28.3 },
    { id: 'south', name: '远岸商城', revenue: 768540, change: 14.2, refund: 39860, refundRate: 5.19, fees: 121430, profit: 194210, profitChange: 16.8 },
    { id: 'camp', name: '山野装备店', revenue: 523700, change: 19.8, refund: 28300, refundRate: 5.41, fees: 86240, profit: 99970, profitChange: 27.6 },
  ],
}

export const adminDemo: Record<string, { title: string; description: string; action: string; columns: string[]; rows: string[][] }> = {
  organization: { title: '组织与店铺', description: '维护企业、经营主体、平台账号与店铺的生效范围。', action: '新增店铺', columns: ['名称','类型','所属范围','状态','生效日期'], rows: [['海风户外用品','企业','—','使用中','2026-01-01'],['海风户外电商主体','经营主体','海风户外用品','使用中','2026-01-01'],['北辰旗舰店','店铺','天猫户外账号','使用中','2026-01-01'],['远岸商城','店铺','京东户外账号','使用中','2026-03-01']] },
  sources: { title: '数据来源', description: '定义需要收集的经营文件，以及它们适用的店铺范围。', action: '新增数据来源', columns: ['名称','收集频率','适用范围','完整性','状态'], rows: [['销售明细','每日','全部店铺','已收到','使用中'],['退款记录','每日','全部店铺','已收到','使用中'],['平台费用','每月','北辰、远岸','待上传','使用中'],['商品成本','不定期','全部店铺','待上传','使用中']] },
  models: { title: '模型与指标', description: '统一管理经营口径；生效后的调整会作为新版本发布。', action: '新建指标', columns: ['名称','业务口径','版本','状态','生效日期'], rows: [['电商标准经营模型','销售、退款、费用、成本与利润','v3','已发布','2026-06-01'],['净销售额','销售额减退款','v2','已发布','2026-06-01'],['经营利润','净销售额减全部经营成本','v4','已发布','2026-06-01']] },
  assets: { title: 'Power BI 资产', description: '企业级文件可同时服务多个店铺与平台；解析失败时可人工登记。', action: '登记 PBIX', columns: ['文件','适用范围','输入要求','检查结果','版本'], rows: [['户外经营分析.pbix','天猫、京东 / 3 家店铺','销售、退款、费用','检查通过','v2'],['管理层月报.pbix','企业范围','经营汇总','等待人工确认','v1']] },
  ai: { title: 'AI 设置', description: 'AI 只提供草案和解释；不可用时经营计算与看板仍正常运行。', action: '添加服务商', columns: ['服务商','运行方式','负责内容','主备策略','状态'], rows: [['企业 LiteLLM 网关','内网','业务问答、异常解释','主模型 + 备用模型','使用中'],['无 AI 模式','禁用','—','确定性流程继续','可切换']] },
  publish: { title: '审核与发布', description: '只有经营数据核对通过后，才会更新正式看板。', action: '审核本月数据', columns: ['期间','适用范围','经营数据核对','看板状态','操作人'], rows: [['2026年6月','全部店铺','7 项全部通过','已发布','张小北'],['2026年7月','全部店铺','等待文件','尚未更新','—']] },
  reports: { title: '高级报表设计', description: '实施人员可进入 Superset 设计图表；业务口径仍由本平台统一管理。', action: '打开 Superset', columns: ['看板','数据范围','设计工具','状态','更新时间'], rows: [['经营总览','全部店铺','Superset','已发布','今天 10:12'],['店铺利润分析','3 家店铺','Superset','设计中','昨天 17:40']] },
  users: { title: '用户与权限', description: '按职责授予看板查看、数据准备、配置管理和发布权限。', action: '邀请用户', columns: ['姓名','角色','数据范围','状态','最近访问'], rows: [['林经理','业务分析','全部店铺','使用中','今天 09:31'],['张小北','管理员','企业范围','使用中','今天 10:21'],['陈实施','实施人员','企业范围','使用中','昨天 18:02']] },
  audit: { title: '审计与备份', description: '查看重要操作记录、备份完整性和系统恢复准备情况。', action: '立即健康检查', columns: ['时间','事项','操作人','结果','范围'], rows: [['今天 10:21','上传销售明细','张小北','完成','北辰旗舰店'],['今天 10:12','自动备份','系统','验证通过','配置、文件与看板'],['昨天 17:40','发布经营模型 v3','陈实施','完成','全部店铺']] },
}
