// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { TrustCenter } from "./TrustCenter";

afterEach(() => {
  vi.restoreAllMocks();
});

test("用店铺月份结论引导经营者打开精确原始依据", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        currentPeriod: "2026-07",
        periods: ["2026-07"],
        stores: [{ id: "store-1", name: "春风旗舰店", platformId: "taobao" }],
        cells: [
          {
            periodId: "period-1",
            storeId: "store-1",
            storeName: "春风旗舰店",
            platformId: "taobao",
            period: "2026-07",
            status: "amount_mismatch",
            statusLabel: "金额对不上",
            runId: "run-1",
            firstReviewId: "review-1",
            periodState: "open",
            facts: {
              requiredSourceCount: 2,
              presentSourceCount: 2,
              missingSourceCount: 0,
              failedSourceCount: 0,
              unresolvedCount: 1,
              unresolvedAmount: "20.0000",
              balanceCount: 6,
              balancedCount: 5,
              amountMatchRate: "0.8333",
              candidateInputCount: 0,
              lastCalculatedAt: "2026-07-26T12:00:00Z"
            },
            explanation: {
              happened: "有 1 笔平台金额没有找到对应记录。",
              impact: "本月金额暂时不能作为正式结果。",
              action: "打开金额最大的原始记录。",
              outcome: "确认后会保留原因并重新核验。"
            },
            checks: [
              {
                key: "sources",
                label: "本月文件",
                state: "passed",
                explanation: "必需文件已经齐全。"
              }
            ]
          }
        ],
        summary: {
          storeCount: 1,
          usableCount: 0,
          attentionCount: 1,
          missingSourceCount: 0,
          amountMismatchCount: 1,
          waitingReviewCount: 0,
          processingCount: 0,
          collectingCount: 0,
          verdict: "当前有 1 家店需要关注。"
        },
        firstAttention: null,
        boundary: "可以使用表示通过当前规则，不代表审计意见。"
      }),
      { headers: { "Content-Type": "application/json" } }
    )
  );
  const onOpenEvidence = vi.fn();
  const onScopeChange = vi.fn();

  render(
    <TrustCenter
      onOpenAnalytics={vi.fn()}
      onOpenEvidence={onOpenEvidence}
      onOpenProgress={vi.fn()}
      onOpenReviews={vi.fn()}
      onScopeChange={onScopeChange}
      refreshVersion={0}
    />
  );

  expect(
    await screen.findByRole("heading", { name: "这个月的数据能不能信？" })
  ).toBeInTheDocument();
  expect((await screen.findAllByText("金额对不上")).length).toBeGreaterThan(0);
  fireEvent.click(
    screen.getByRole("button", { name: "查看这笔原始记录" })
  );
  expect(onOpenEvidence).toHaveBeenCalledWith("review-1");
  expect(onScopeChange).toHaveBeenCalledWith(
    expect.objectContaining({ storeId: "store-1", period: "2026-07" })
  );
});
