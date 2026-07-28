// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { PerformancePanel } from "./PerformancePanel";

afterEach(() => {
  vi.restoreAllMocks();
});

test("历史绩效即使引擎门禁通过也不会冒充认证绩效", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    const payload = path.includes("/performance/overview")
      ? {
          status: "reference_ready",
          calculationMode: "single",
          period: "2606",
          referenceOnly: true,
          certifiedPerformanceAvailable: false,
          engineGate: {
            status: "certified",
            message: "确定性商品账已通过",
            code: null,
            details: {}
          },
          rowCount: 10,
          storeCount: 1,
          personCount: 1,
          productCount: 2,
          formulaPassCount: 10,
          formulaPassRate: "1",
          metrics: {
            collectedAmount: "100.0000",
            refundAmount: "10.0000",
            productCost: "30.0000",
            advertisingFee: "5.0000",
            storeProfit: "55.0000"
          },
          assignment: {
            activeCount: 2,
            conflictCount: 0,
            latestEffectiveDate: "2026-06-01",
            provisionalPersonCount: 0
          }
        }
      : {
          period: "2606",
          calculationMode: "single",
          referenceOnly: true,
          rows: [
            {
              personId: "person-1",
              personName: "负责人甲",
              storeName: "测试店",
              productCount: 2,
              collectedAmount: "100.0000",
              refundAmount: "10.0000",
              productCost: "30.0000",
              advertisingFee: "5.0000",
              storeProfit: "55.0000",
              failedFormulaRows: 0
            }
          ]
        };
    return new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" }
    });
  });

  render(<PerformancePanel />);

  expect(await screen.findByText("当前只显示历史对照")).toBeInTheDocument();
  expect(screen.getByText("历史店铺利润")).toBeInTheDocument();
  expect(screen.queryByText("当前范围已有认证绩效")).not.toBeInTheDocument();
  expect(screen.queryByText("认证经营利润")).not.toBeInTheDocument();
});
