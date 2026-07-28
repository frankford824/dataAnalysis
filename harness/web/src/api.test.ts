// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";

import { decideReview, loadProgress } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

test("实时进度请求带上当前店铺和月份", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        shop: "一店",
        period: "2026-03",
        periodState: "open",
        gates: [],
        sourceCount: 0,
        unresolvedCount: 0,
        unexplainedAmount: "0.0000"
      }),
      { headers: { "Content-Type": "application/json" } }
    )
  );

  await loadProgress({ storeId: "store-1", period: "2603" });

  expect(fetchSpy).toHaveBeenCalledWith(
    "/api/v1/progress?storeId=store-1&period=2603",
    expect.objectContaining({ credentials: "same-origin" })
  );
});

test("服务端故障不会把路径或数据库错误暴露给普通页面", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response("Binder Error: /workbench/private/ledger.duckdb", {
      status: 500
    })
  );

  await expect(
    decideReview("review-1", "explain", "人工已核对")
  ).rejects.toThrow("保存说明失败");
  await expect(
    decideReview("review-1", "explain", "人工已核对")
  ).rejects.not.toThrow(/workbench|Binder Error/);
});
