import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
  waitFor
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import App, { displayPeriod } from "./App";
import type { AnalyticsCatalog, AnalyticsOverview } from "./types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("月份文案兼容任务接口的紧凑年月", () => {
  expect(displayPeriod("2607")).toBe("2026年7月（进行中）");
  expect(displayPeriod("202607")).toBe("2026年7月（进行中）");
  expect(displayPeriod("2026-06")).toBe("2026年6月");
  expect(displayPeriod("unknown")).toBe("unknown");
});

test("服务失败时不使用示例数据", async () => {
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("服务离线"));
  render(<App />);

  expect(await screen.findByText("无法连接对账服务")).toBeInTheDocument();
  expect(screen.getByText("服务离线")).toBeInTheDocument();
  expect(screen.queryByText("¥ 900.00")).not.toBeInTheDocument();
});

test("待处理差额分批展示且可继续加载", async () => {
  const reviews = Array.from({ length: 30 }, (_, index) => ({
    unresolvedId: `unresolved-${index}`,
    reasonCode: "amount_mismatch",
    amount: `${index + 1}.0000`,
    status: "open",
    businessTitle: "订单与平台钱包金额不一致",
    businessSummary: "同一笔业务在订单明细和平台钱包中的金额没有完全对上。",
    suggestedAction: "核对退款、平台费用、优惠补贴和跨月结算。",
    evidenceCount: 1,
    storeId: "store-test",
    storeName: "测试店铺",
    period: "2604"
  }));
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    const payload = path.endsWith("/status")
      ? {
          mode: "real",
          workspace: "/workbench",
          schemaVersion: 10,
          llmEnabled: false,
          llmConfigured: false,
          autonomyLevel: "L0",
          reconciliationMode: "platform_wallet",
          bankCashStatus: "not_applicable",
          readOnlySourceEnforced: true,
          updatedAt: "2026-07-24T00:00:00Z"
        }
      : path.endsWith("/progress")
        ? {
            shop: "测试店铺",
            period: "2026-04",
            periodState: "open",
            gates: [],
            sourceCount: 2,
            unresolvedCount: 30,
            unexplainedAmount: "465.0000"
          }
        : path.includes("/reviews/page?")
          ? {
              total: reviews.length,
              offset: 0,
              limit: 100,
              items: reviews,
              hasMore: false
            }
          : [];
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  });
  render(<App />);

  const pendingButton = await screen.findByRole("button", {
    name: "待处理 30"
  });
  fireEvent.click(pendingButton);
  fireEvent.click(screen.getByRole("button", { name: /需要核对的问题/ }));
  const differencesDialog = screen.getByRole("dialog", { name: "需要核对的问题" });

  expect(
    await within(differencesDialog).findAllByText("订单与平台钱包金额不一致")
  ).toHaveLength(25);
  expect(within(differencesDialog).queryByText("¥ 30.00")).not.toBeInTheDocument();
  fireEvent.click(within(differencesDialog).getByRole("button", { name: "再显示 25 项" }));
  expect(
    await within(differencesDialog).findAllByText("订单与平台钱包金额不一致")
  ).toHaveLength(30);
  expect(within(differencesDialog).getByText("¥ 30.00")).toBeInTheDocument();
});

test("大量行级问题先按店铺月份和业务原因归并再下钻", async () => {
  const review = {
    unresolvedId: "grouped-1",
    reasonCode: "amount_mismatch",
    amount: "30.0000",
    status: "open",
    businessTitle: "订单与平台钱包金额不一致",
    businessSummary: "同一笔业务在订单明细和平台钱包中的金额没有完全对上。",
    suggestedAction: "核对退款、平台费用、优惠补贴和跨月结算。",
    evidenceCount: 1,
    storeId: "store-test",
    storeName: "测试店铺",
    period: "2604"
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    const payload = path.endsWith("/status")
      ? dashboardStatus
      : path.endsWith("/progress")
        ? {
            ...dashboardProgress,
            unresolvedCount: 1000,
            unexplainedAmount: "30000.0000"
          }
        : path.includes("/reviews/groups?")
          ? {
              groupCount: 1,
              recordCount: 1000,
              groups: [
                {
                  groupId: "group-1",
                  storeId: "store-test",
                  storeName: "测试店铺",
                  period: "2604",
                  reasonCode: "amount_mismatch",
                  businessTitle: "订单与平台钱包金额不一致",
                  businessSummary: review.businessSummary,
                  suggestedAction: review.suggestedAction,
                  itemCount: 1000,
                  totalAmount: "30000.0000",
                  absoluteAmount: "30000.0000",
                  evidenceCount: 1000
                }
              ]
            }
          : path.includes("/reviews/page?") &&
              path.includes("reasonCode=amount_mismatch")
            ? {
                total: 1,
                offset: 0,
                limit: 100,
                items: [review],
                hasMore: false
              }
            : path.includes("/reviews/page?")
              ? {
                  total: 1000,
                  offset: 0,
                  limit: 100,
                  items: [review],
                  hasMore: true
                }
              : [];
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  });
  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "待处理 1千+" }));
  fireEvent.click(screen.getByRole("button", { name: /需要核对的问题/ }));
  const groupsDialog = screen.getByRole("dialog", { name: "需要核对的问题" });
  expect(
    within(groupsDialog).getByText("系统已按店铺、月份和业务原因归并原始记录。", {
      exact: false
    })
  ).toBeInTheDocument();
  fireEvent.click(
    within(groupsDialog).getByRole("button", {
      name: /订单与平台钱包金额不一致/
    })
  );
  expect(
    await screen.findByRole("dialog", { name: "订单与平台钱包金额不一致" })
  ).toBeInTheDocument();
  expect(await screen.findByText("返回问题分组")).toBeInTheDocument();
  expect(await screen.findByText("¥ 30.00")).toBeInTheDocument();
});

test("待处理记录只展示业务语言且模型建议不会直接写金额", async () => {
  const review = {
    unresolvedId: "unresolved-business-copy",
    reasonCode: "missing_side",
    amount: "26908.1400",
    status: "open",
    businessTitle: "平台钱包有记录，订单明细未找到",
    businessSummary: "平台钱包中存在这笔收支，但本月订单文件里没有找到对应订单。",
    suggestedAction: "先检查订单文件是否完整，再核对退款、撤销订单或跨月到账情况。",
    evidenceCount: 1,
    storeId: "store-test",
    storeName: "测试店铺",
    period: "2604"
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/reviews/unresolved-business-copy/suggestion")) {
      return new Response(
        JSON.stringify({
          status: "suggestion",
          suggestion: "建议先核对退款记录和交易日期，再确认是否属于跨月到账。",
          model: "test-model",
          requestId: "request-1",
          suggestionId: "suggestion-1",
          evidenceGuard: "passed",
          reviewerModel: "reviewer-model",
          reviewerStatus: "failed",
          reviewerReason: "原始记录只能证明钱包侧存在，不能证明订单侧缺失。",
          mayWriteLedger: false,
          requiresHumanReview: true
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    const payload = path.endsWith("/status")
      ? {
          ...dashboardStatus,
          llmEnabled: true,
          llmConfigured: true
        }
      : path.endsWith("/progress")
        ? {
            ...dashboardProgress,
            unresolvedCount: 1,
            unexplainedAmount: "26908.1400"
          }
        : path.includes("/reviews/page?") && method === "GET"
          ? {
              total: 1,
              offset: 0,
              limit: 100,
              items: [review],
              hasMore: false
            }
          : [];
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  });
  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "待处理 1" }));
  fireEvent.click(screen.getByRole("button", { name: /需要核对的问题/ }));
  const dialog = screen.getByRole("dialog", { name: "需要核对的问题" });
  await within(dialog).findByText("平台钱包有记录，订单明细未找到");
  expect(
    within(dialog).getByText(
      (_content, element) =>
        element?.tagName === "SMALL" &&
        Boolean(
          element.textContent?.includes(
            "平台钱包中存在这笔收支，但本月订单文件里没有找到对应订单。"
          )
        )
    )
  ).toBeInTheDocument();
  expect(screen.queryByText(/bridge_ids|missing_sides|rule_versions/)).not.toBeInTheDocument();

  fireEvent.click(within(dialog).getByText("平台钱包有记录，订单明细未找到"));
  expect(
    screen.getByText("先检查订单文件是否完整，再核对退款、撤销订单或跨月到账情况。")
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "用模型生成说明草案" }));

  expect(
    await screen.findByDisplayValue(
      "建议先核对退款记录和交易日期，再确认是否属于跨月到账。"
    )
  ).toBeInTheDocument();
  expect(
    screen.getByText("独立复核认为证据不足", { exact: false })
  ).toBeInTheDocument();
});

test("钱包模式明确展示核对范围且不伪造银行资金腿", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    const payload = path.endsWith("/status")
      ? {
          mode: "real",
          workspace: "/workbench",
          schemaVersion: 10,
          llmEnabled: false,
          llmConfigured: false,
          autonomyLevel: "L0",
          reconciliationMode: "platform_wallet",
          bankCashStatus: "not_applicable",
          readOnlySourceEnforced: true,
          updatedAt: "2026-07-24T00:00:00Z"
        }
      : path.endsWith("/progress")
        ? {
            shop: "测试店铺",
            period: "2026-04",
            periodState: "open",
            gates: [],
            sourceCount: 2,
            unresolvedCount: 0,
            unexplainedAmount: "0.0000"
          }
        : [];
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  });
  render(<App />);

  fireEvent.click(await screen.findByText("更多核验信息"));
  fireEvent.click(await screen.findByRole("button", { name: "实时进度" }));
  fireEvent.click(await screen.findByRole("button", { name: "查看全部" }));
  expect(
    await screen.findByText("订单 + 支付宝/微信平台钱包")
  ).toBeInTheDocument();
  expect(screen.getByText("当前不纳入核对")).toBeInTheDocument();
  expect(screen.queryByText("本账期必须提供")).not.toBeInTheDocument();
});

test("处理进度适配 compute 字段并可启动全部真实范围", async () => {
  const requests: Array<{ path: string; method: string }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    requests.push({ path, method });
    const payload = path.endsWith("/compute/run") && method === "POST"
      ? {
          accepted: true,
          running: false,
          message: "已开始检查全部店铺和月份。"
        }
      : path.endsWith("/compute/targets")
        ? {
            scope_start: "2026-02-01",
            scope_end: "2026-07-24",
            targets: [
              {
                target_key: "douyin:store-1:2026-07",
                platform: "抖音电商",
                logical_store: "远山生活店",
                logical_store_key: "store-1",
                period: "2026-07",
                status: "available",
                period_state: "partial",
                source_ids: ["source-1"],
                evidence: ["orders.xlsx"],
                aliases: []
              }
            ],
            review_required: []
          }
        : path.includes("/compute/jobs")
          ? [
              {
                jobId: "job-2",
                cycleId: "cycle-1",
                kind: "reconcile",
                storeId: "store-1",
                period: "2026-07",
                status: "succeeded",
                progressPercent: 100,
                label: "远山生活店 · 2026年7月核对",
                detail: "已完成",
                createdAt: "2026-07-24T01:00:00Z",
                startedAt: "2026-07-24T01:00:01Z",
                finishedAt: "2026-07-24T01:00:02Z",
                error: null
              }
            ]
          : path.endsWith("/status")
      ? dashboardStatus
      : path.endsWith("/progress")
        ? {
            ...dashboardProgress,
            shop: "远山生活店",
            period: "2026-07",
            compute: {
              enabled: true,
              running: true,
              cycleId: "cycle-1",
              total: 18,
              queued: 7,
              active: 2,
              succeeded: 8,
              failed: 1,
              current: [
                {
                  jobId: "job-1",
                  label: "远山生活店 · 2026年7月核对",
                  detail: "正在核对订单与平台钱包",
                  status: "running",
                  progressPercent: 45,
                  storeId: "store-1",
                  period: "2026-07"
                }
              ]
            }
          }
        : [];
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  });
  render(<App />);

  fireEvent.click(await screen.findByText("更多核验信息"));
  fireEvent.click(await screen.findByRole("button", { name: "实时进度" }));
  expect(
    await screen.findByRole("heading", {
      name: "远山生活店 · 2026年7月（进行中）"
    })
  ).toBeInTheDocument();
  expect(screen.getByText("总任务")).toBeInTheDocument();
  expect(screen.getByText("18")).toBeInTheDocument();
  expect(screen.getByText("7")).toBeInTheDocument();
  expect(screen.getByText("2")).toBeInTheDocument();
  expect(screen.getByText("8")).toBeInTheDocument();
  expect(screen.getByText("1")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "正在处理的范围" }));
  expect(
    await screen.findByText(
      "抖音电商 · 2026年7月（进行中） · 正在核对订单与平台钱包"
    )
  ).toBeInTheDocument();
  expect(screen.getByText("1 个店铺，1 个店铺月份；2026年7月（进行中）。")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭正在处理的范围" }));
  fireEvent.click(screen.getByRole("button", { name: "最近处理结果" }));
  expect(screen.getByRole("dialog", { name: "最近处理结果" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭最近处理结果" }));

  fireEvent.click(screen.getByRole("button", { name: "再次检查新文件" }));
  expect(
    await screen.findByText("已开始检查全部店铺和月份。")
  ).toBeInTheDocument();
  await waitFor(() => {
    expect(
      requests.some(
        (request) =>
          request.path.endsWith("/api/v1/compute/run") &&
          request.method === "POST"
      )
    ).toBe(true);
  });
});

const dashboardStatus = {
  mode: "real",
  workspace: "/workbench",
  schemaVersion: 10,
  llmEnabled: false,
  llmConfigured: false,
  autonomyLevel: "L0",
  reconciliationMode: "platform_wallet",
  bankCashStatus: "not_applicable",
  readOnlySourceEnforced: true,
  updatedAt: "2026-07-24T00:00:00Z"
};

const dashboardProgress = {
  shop: "测试店铺",
  period: "2026-04",
  periodState: "open",
  gates: [],
  sourceCount: 2,
  unresolvedCount: 0,
  unexplainedAmount: "0.0000"
};

const inputRevisionGroup = {
  groupId: "wechat-2026-04",
  period: "2026-04",
  sourceKind: "wechat_ledger",
  label: "微信账单",
  candidates: [
    {
      revisionId: "revision-raw",
      originalName: "微信支付账单-原始导出.xlsx",
      sourceLabel: "平台原始导出",
      status: "candidate",
      reason: "与本月另一份微信账单内容不同",
      rowCount: 2701
    },
    {
      revisionId: "revision-edited",
      originalName: "C:\\finance\\2604微信收款.xlsx",
      sourceLabel: "D:\\财务\\处理结果",
      status: "candidate",
      reason: "内容与同月另一版本不同",
      rowCount: 2700
    }
  ]
};

const capabilityStatus = {
  effectiveLevel: "L0",
  levelReason:
    "能力范围可以扩展，但模型仍只生成建议；金额、规则发布和绩效结果由确定性代码控制。",
  modelEnabled: false,
  tasks: [
    {
      id: "evidence_locator",
      name: "定位原始文件与行",
      state: "active",
      usesModel: false,
      mayWriteLedger: false
    },
    {
      id: "difference_diagnosis",
      name: "差额原因草案",
      state: "model_disabled",
      usesModel: true,
      mayWriteLedger: false
    },
    {
      id: "performance_attribution",
      name: "人员、店铺与商品归属",
      state: "reference_validation",
      usesModel: false,
      mayWriteLedger: false
    }
  ],
  learning: {
    suggestionCount: 4,
    reviewedCount: 2,
    correctionCount: 1,
    evidenceGuardedCount: 3,
    promotionEligible: false,
    promotionReason: "尚未形成跨账期盲测真值；人工接受率不能冒充准确率。"
  }
};

function dashboardResponse(path: string, inputRevisionGroups = [inputRevisionGroup]) {
  const payload = path.endsWith("/status")
    ? dashboardStatus
    : path.endsWith("/progress")
      ? dashboardProgress
      : path.endsWith("/input-revisions")
        ? inputRevisionGroups
        : path.includes("/reviews/page?")
          ? {
              total: 0,
              offset: 0,
              limit: 100,
              items: [],
              hasMore: false
            }
        : path.endsWith("/capabilities")
          ? capabilityStatus
        : [];
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status: 200
  });
}

const analyticsOverview: AnalyticsOverview = {
  filters: {
    platforms: [
      { id: "taobao", name: "淘宝天猫" },
      { id: "douyin", name: "抖音电商" }
    ],
    stores: [
      {
        id: "store-1",
        name: "春风旗舰店",
        platformId: "taobao",
        platformName: "淘宝天猫"
      },
      {
        id: "store-2",
        name: "远山生活店",
        platformId: "douyin",
        platformName: "抖音电商"
      }
    ],
    periods: [
      { value: "2026-04", label: "2026年4月" },
      { value: "2026-05", label: "2026年5月" },
      { value: "2026-07", label: "2026年7月" }
    ],
    dateRange: { min: "2026-04-01", max: "2026-05-31" }
  },
  selection: {
    storeId: "all",
    period: "all",
    fromDate: null,
    toDate: null
  },
  coverage: {
    status: "review_required",
    profitStatus: "historical_pending",
    message: "当前核对尚未达到正式确认条件；金额仅供核对。"
  },
  metrics: {
    orderGross: "1200.0000",
    refunds: "80.0000",
    netSales: "1120.0000",
    walletNet: "1098.5000",
    orderCount: 12,
    transactionCount: 18,
    unresolvedAmount: "21.5000"
  },
  trend: [
    {
      date: "2026-05-01",
      orderGross: "700.0000",
      refunds: "50.0000",
      netSales: "650.0000",
      walletNet: "640.0000"
    },
    {
      date: "2026-05-02",
      orderGross: "500.0000",
      refunds: "30.0000",
      netSales: "470.0000",
      walletNet: "458.5000"
    }
  ],
  storeBreakdown: [
    {
      storeId: "store-1",
      storeName: "春风旗舰店",
      orderGross: "800.0000",
      refunds: "50.0000",
      netSales: "750.0000",
      walletNet: "735.0000",
      orderCount: 8,
      transactionCount: 12
    },
    {
      storeId: "store-2",
      storeName: "远山生活店",
      orderGross: "400.0000",
      refunds: "30.0000",
      netSales: "370.0000",
      walletNet: "363.5000",
      orderCount: 4,
      transactionCount: 6
    }
  ],
  transactions: [
    {
      occurredAt: "2026-05-02T14:25:00",
      storeId: "store-2",
      storeName: "远山生活店",
      sourceKind: "wechat_ledger",
      sourceLabel: "微信流水",
      amount: "-30.0000",
      direction: "expense",
      businessDescription: "顾客退款",
      businessKey: "order-2"
    }
  ],
  monthlyPnl: []
};

const analyticsCatalog: AnalyticsCatalog = {
  allRecordCount: 96,
  candidateRecordCount: 50,
  discoveredStoreCount: 3,
  processedStoreCount: 2,
  platforms: [
    { id: "taobao", name: "淘宝天猫" },
    { id: "douyin", name: "抖音电商" },
    { id: "pinduoduo", name: "拼多多" }
  ],
  stores: [
    {
      id: "store-1",
      name: "春风旗舰店",
      platformId: "taobao",
      platformName: "淘宝天猫",
      periods: ["2026-04", "2026-05"],
      fileCount: 20,
      processed: true
    },
    {
      id: "store-2",
      name: "远山生活店",
      platformId: "douyin",
      platformName: "抖音电商",
      periods: ["2026-05", "2026-07"],
      fileCount: 12,
      processed: true
    },
    {
      id: "store-3",
      name: "尚未处理店铺",
      platformId: "pinduoduo",
      platformName: "拼多多",
      periods: ["2026-07"],
      fileCount: 4,
      processed: false
    }
  ]
};

function analyticsResponse(path: string, overview = analyticsOverview) {
  if (path.includes("/analytics/overview?")) {
    return new Response(JSON.stringify(overview), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  }
  if (path.endsWith("/analytics/catalog")) {
    return new Response(JSON.stringify(analyticsCatalog), {
      headers: { "Content-Type": "application/json" },
      status: 200
    });
  }
  return dashboardResponse(path, []);
}

test("经营看板由真实接口结果驱动并诚实展示覆盖与利润状态", async () => {
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    calls.push(path);
    return analyticsResponse(path);
  });
  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "经营看板" })
  );

  expect(
    await screen.findByRole("heading", { name: "经营看板" })
  ).toBeInTheDocument();
  expect(await screen.findByText("¥ 1,200.00")).toBeInTheDocument();
  expect(screen.getByText("¥ 80.00")).toBeInTheDocument();
  expect(screen.getByText("¥ 1,120.00")).toBeInTheDocument();
  expect(screen.getByText("¥ 1,098.50")).toBeInTheDocument();
  expect(screen.getByText("12")).toBeInTheDocument();
  expect(screen.getByText("历史结果待复核")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "每日经营趋势" })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: /每日经营趋势/ })).toBeInTheDocument();
  expect(screen.getAllByText("顾客退款")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "查看店铺对比" }));
  expect(screen.getByRole("dialog", { name: "店铺经营对比" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭店铺经营对比" }));
  fireEvent.click(screen.getByRole("button", { name: "查看数据覆盖" }));
  expect(screen.getByText("还有 1 个已发现店铺尚未完成处理", { exact: false }))
    .toBeInTheDocument();
  expect(screen.getByText("查看已发现的 3 个店铺")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭当前数据覆盖" }));
  expect(
    calls.some(
      (path) =>
        path.includes(
          "/api/v1/analytics/overview?storeId=all&period=all&limit=50"
        ) &&
        path.includes("fromDate=2026-02-01") &&
        /toDate=\d{4}-\d{2}-\d{2}/.test(path)
    )
  ).toBe(true);
  expect(calls).toContain("/api/v1/analytics/catalog");
  fireEvent.click(screen.getByRole("button", { name: "筛选范围" }));
  expect(screen.getByRole("option", { name: "2026年7月（进行中）" }))
    .toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: "春风旗舰店 · 淘宝 / 天猫" })
  ).toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: "远山生活店 · 抖音电商" })
  ).toBeInTheDocument();
});

test("数据目录按真实平台店铺和月份筛选", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
    analyticsResponse(String(input))
  );
  render(<App />);

  fireEvent.click(await screen.findByText("更多核验信息"));
  fireEvent.click(await screen.findByRole("button", { name: "数据覆盖" }));
  expect(
    await screen.findByRole("heading", { name: "平台、店铺与月份" })
  ).toBeInTheDocument();
  expect(screen.getByText("春风旗舰店")).toBeInTheDocument();
  expect(screen.getByText("远山生活店")).toBeInTheDocument();
  expect(screen.getByText("尚未处理店铺")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "筛选并查看全部" }));
  const catalogDialog = screen.getByRole("dialog", { name: "筛选店铺与月份" });
  fireEvent.change(screen.getByLabelText("平台"), {
    target: { value: "pinduoduo" }
  });
  expect(within(catalogDialog).queryByText("春风旗舰店")).not.toBeInTheDocument();
  expect(within(catalogDialog).getByText("尚未处理店铺")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("月份"), {
    target: { value: "2026-07" }
  });
  expect(within(catalogDialog).getByText("尚未处理店铺")).toBeInTheDocument();
  expect(within(catalogDialog).getAllByText("2026年7月（进行中）")).toHaveLength(2);
});

test("店铺月份和日期过滤会重新请求并更新经营结果", async () => {
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    calls.push(path);
    if (
      path.includes("/analytics/overview?") &&
      path.includes("storeId=store-2")
    ) {
      return analyticsResponse(path, {
        ...analyticsOverview,
        selection: {
          storeId: "store-2",
          period: path.includes("period=2026-05") ? "2026-05" : "all",
          fromDate: path.includes("fromDate=2026-05-01") ? "2026-05-01" : null,
          toDate: path.includes("toDate=2026-05-31") ? "2026-05-31" : null
        },
        metrics: {
          ...analyticsOverview.metrics,
          orderGross: "420.0000",
          refunds: "30.0000",
          netSales: "390.0000",
          walletNet: "382.5000",
          orderCount: 4
        },
        storeBreakdown: [analyticsOverview.storeBreakdown[1]]
      });
    }
    return analyticsResponse(path);
  });
  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "经营看板" })
  );

  const filterButton = await screen.findByRole("button", { name: "筛选范围" });
  await waitFor(() => expect(filterButton).toBeEnabled());
  fireEvent.click(filterButton);
  await screen.findByRole("option", { name: "远山生活店 · 抖音电商" });
  const storeSelect = screen.getByLabelText("店铺");
  expect(storeSelect).toBeEnabled();
  fireEvent.change(storeSelect, { target: { value: "store-2" } });

  fireEvent.change(screen.getByLabelText("月份"), {
    target: { value: "2026-05" }
  });
  fireEvent.change(screen.getByLabelText("起始日期"), {
    target: { value: "2026-05-01" }
  });
  fireEvent.change(screen.getByLabelText("结束日期"), {
    target: { value: "2026-05-31" }
  });
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
  expect(await screen.findByText("¥ 420.00")).toBeInTheDocument();
  await waitFor(() =>
    expect(
      calls.some(
        (path) =>
          path.includes("storeId=store-2") &&
          path.includes("period=2026-05") &&
          path.includes("fromDate=2026-05-01") &&
          path.includes("toDate=2026-05-31")
      )
    ).toBe(true)
  );
});

test("经营看板对无数据范围展示真实空状态", async () => {
  const emptyOverview = {
    ...analyticsOverview,
    coverage: {
      status: "no_data",
      profitStatus: "historical_pending",
      message: "当前范围还没有成功完成的核对结果。"
    },
    metrics: {
      orderGross: "0.0000",
      refunds: "0.0000",
      netSales: "0.0000",
      walletNet: "0.0000",
      orderCount: 0,
      transactionCount: 0
    },
    trend: [],
    storeBreakdown: [],
    transactions: [],
    monthlyPnl: []
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
    analyticsResponse(String(input), emptyOverview)
  );
  render(<App />);
  fireEvent.click(
    await screen.findByRole("button", { name: "经营看板" })
  );

  expect(
    await screen.findByText(
      "当前筛选范围还没有成功完成的处理结果。请调整店铺、月份或日期。"
    )
  ).toBeInTheDocument();
  expect(screen.queryByText("¥ 0.00")).not.toBeInTheDocument();
  expect(screen.getByText("当前范围暂无结果")).toBeInTheDocument();
});

test("证据不足的文件版本进入学习记录而不要求用户猜选", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
    dashboardResponse(String(input))
  );
  render(<App />);

  const pendingButton = await screen.findByRole("button", {
    name: "待处理 1"
  });
  fireEvent.click(pendingButton);
  fireEvent.click(screen.getByRole("button", { name: /文件版本待学习/ }));

  expect(
    await screen.findByRole("heading", { name: "待学习的文件版本" })
  ).toBeInTheDocument();
  expect(screen.getByText("微信支付账单-原始导出.xlsx")).toBeInTheDocument();
  expect(screen.getByText("2604微信收款.xlsx")).toBeInTheDocument();
  expect(screen.getByText("2,701 行", { exact: false })).toBeInTheDocument();
  expect(screen.getByText("来源位置已隐藏", { exact: false })).toBeInTheDocument();
  expect(screen.queryByText("C:\\finance\\2604微信收款.xlsx")).not.toBeInTheDocument();
  expect(screen.queryByText("D:\\财务\\处理结果")).not.toBeInTheDocument();
  expect(screen.getAllByText("保留证据，暂不入账")).toHaveLength(2);
  expect(
    screen.queryByRole("button", { name: "作为本月原始文件" })
  ).not.toBeInTheDocument();
  expect(
    screen.getByText("系统不会让你凭文件名猜", { exact: false })
  ).toBeInTheDocument();
});

test("无模型配置时明确保持确定性核对可用", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (path.endsWith("/llm/config")) {
      return new Response(
        JSON.stringify({
          enabled: false,
          configured: false,
          protocol: null,
          baseUrl: null,
          selectedModel: null,
          keyConfigured: false,
          completionSupported: false,
          detail: "未配置模型服务；确定性流程正常可用",
          updatedAt: null
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    return dashboardResponse(path, []);
  });
  render(<App />);

  fireEvent.click(
    await screen.findByRole("button", { name: "模型辅助" })
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "查看能力与学习门禁" })
  );
  expect(
    await screen.findByRole("heading", {
      name: "模型现在能帮你做什么"
    })
  ).toBeInTheDocument();
  expect(screen.getByText("定位原始文件与行")).toBeInTheDocument();
  expect(screen.getByText("人员、店铺与商品归属")).toBeInTheDocument();
  expect(screen.getByText("人工接受率不能冒充准确率。", { exact: false })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "配置模型连接" }));

  expect(await screen.findByText("未启用模型")).toBeInTheDocument();
  expect(
    screen.getByText("未配置或调用失败时不影响对账。", { exact: false })
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "检测可用模型" })
  ).toBeDisabled();
});

test("可识别 OpenAI 协议模型并应用后立即生效", async () => {
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  let applied = false;
  let tested = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path.endsWith("/llm/config") && method === "GET") {
      return new Response(
        JSON.stringify(
          applied
            ? {
                enabled: true,
                configured: true,
                protocol: "openai_compatible",
                baseUrl: "https://gateway.example/v1",
                selectedModel: "gpt-test-large",
                keyConfigured: true,
                completionSupported: true,
                detail: "已启用",
                updatedAt: "2026-07-24T01:00:00Z",
                lastTaskStatus: tested ? "ok" : "pending",
                lastTaskPurpose: tested ? "connection_test" : null,
                lastTaskModel: "gpt-test-large",
                lastTaskMessage: tested ? "模型已实际响应。" : null,
                lastTaskAt: tested ? "2026-07-24T01:01:00Z" : null
              }
            : {
                enabled: false,
                configured: false,
                protocol: null,
                baseUrl: null,
                selectedModel: null,
                keyConfigured: false,
                completionSupported: false,
                detail: "未配置模型服务；确定性流程正常可用",
                updatedAt: null
              }
        ),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    if (path.endsWith("/llm/discover") && method === "POST") {
      calls.push({
        url: path,
        method,
        body: JSON.parse(String(init?.body))
      });
      return new Response(
        JSON.stringify({
          protocol: "openai_compatible",
          baseUrl: "https://gateway.example/v1",
          models: ["gpt-test-small", "gpt-test-large"],
          completionSupported: true
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    if (path.endsWith("/llm/config") && method === "PUT") {
      applied = true;
      calls.push({
        url: path,
        method,
        body: JSON.parse(String(init?.body))
      });
      return new Response(
        JSON.stringify({
          enabled: true,
          configured: true,
          protocol: "openai_compatible",
          baseUrl: "https://gateway.example/v1",
          selectedModel: "gpt-test-large",
          reviewerModel: "gpt-test-small",
          keyConfigured: true,
          completionSupported: true,
          detail: "已启用",
          updatedAt: "2026-07-24T01:00:00Z"
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    if (path.endsWith("/llm/test") && method === "POST") {
      tested = true;
      calls.push({ url: path, method, body: null });
      return new Response(
        JSON.stringify({
          status: "ok",
          model: "gpt-test-large",
          message: "模型已实际响应，可用于生成业务说明草案。",
          requestId: "request-test"
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    return dashboardResponse(path, []);
  });
  render(<App />);

  fireEvent.click(
    await screen.findByRole("button", { name: "模型辅助" })
  );
  fireEvent.click(screen.getByRole("button", { name: "配置模型连接" }));
  fireEvent.change(await screen.findByLabelText("接口地址"), {
    target: { value: "https://gateway.example/v1" }
  });
  fireEvent.change(screen.getByLabelText("API Key"), {
    target: { value: "secret-for-test" }
  });
  fireEvent.click(screen.getByRole("button", { name: "检测可用模型" }));

  expect(
    await screen.findByText("已识别 2 个可用模型，请选择后应用。")
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("负责提出解释的模型"), {
    target: { value: "gpt-test-large" }
  });
  fireEvent.change(screen.getByLabelText("负责独立挑错的模型（推荐）"), {
    target: { value: "gpt-test-small" }
  });
  fireEvent.click(
    screen.getByRole("button", { name: "应用并立即生效" })
  );

  expect(
    await screen.findByText(
      "已应用 gpt-test-large，并完成一次真实模型调用。现在可在“待处理”中生成业务说明草案。"
    )
  ).toBeInTheDocument();
  expect(screen.getByText("gpt-test-large 已生效")).toBeInTheDocument();
  expect(screen.getByText("连接验证")).toBeInTheDocument();
  expect(screen.getByText("成功")).toBeInTheDocument();
  expect(screen.getByText("模型已实际响应。")).toBeInTheDocument();
  expect(calls).toEqual([
    {
      url: "/api/v1/llm/discover",
      method: "POST",
      body: {
        protocol: "auto",
        baseUrl: "https://gateway.example/v1",
        apiKey: "secret-for-test"
      }
    },
    {
      url: "/api/v1/llm/config",
      method: "PUT",
      body: {
        protocol: "openai_compatible",
        baseUrl: "https://gateway.example/v1",
        apiKey: "secret-for-test",
        selectedModel: "gpt-test-large",
        reviewerModel: "gpt-test-small",
        enabled: true
      }
    },
    {
      url: "/api/v1/llm/test",
      method: "POST",
      body: null
    }
  ]);
  expect(screen.getByLabelText(/API Key/)).toHaveValue("");
});
