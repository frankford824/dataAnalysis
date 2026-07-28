// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("./EvidenceWorkbook", () => ({
  default: ({
    originalName,
    targetRow
  }: {
    originalName: string;
    targetRow: number;
  }) => <div>只读表格：{originalName}，第 {targetRow} 行</div>
}));

import { EvidenceWorkbench } from "./EvidenceWorkbench";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

test("从待确认记录打开原文件并用业务语言解释差额", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = String(input);
    if (path.endsWith("/reviews/review-1/evidence")) {
      return new Response(
        JSON.stringify({
          unresolvedId: "review-1",
          balanceId: "balance-1",
          lineageStatus: "frozen",
          sources: [
            {
              snapshotId: "snapshot-1",
              artifactId: "artifact-1",
              originalName: "三月订单.csv",
              sourceMember: null,
              sourceSheet: null,
              rowNumber: 3,
              field: "金额",
              normalizedValue: "20.00",
              normalizationVersion: "normalize-v1",
              ruleVersionId: "rule-v1",
              sourceKind: "orders"
            }
          ]
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }
    return new Response(
      JSON.stringify({
        unresolvedId: "review-1",
        snapshotId: "snapshot-1",
        lineageStatus: "frozen",
        contentSha256: "a".repeat(64),
        fileKind: "csv",
        memberName: null,
        sheetNames: ["三月订单.csv"],
        originalName: "三月订单.csv",
        readOnly: true,
        formulasAreDeterministic: false,
        sheet: {
          name: "三月订单.csv",
          hidden: false,
          window: {
            headerRowNumber: 1,
            targetRowNumber: 3,
            targetDataRowNumber: 2,
            startRowNumber: 2,
            endRowNumber: 3,
            targetColumnIndex: 2,
            columns: [
              { index: 0, label: "订单号", sourceLabel: "订单号" },
              { index: 1, label: "店铺", sourceLabel: "店铺" },
              { index: 2, label: "金额", sourceLabel: "金额" }
            ],
            rows: [
              {
                sourceRowNumber: 2,
                sourceEndRowNumber: 2,
                dataRowNumber: 1,
                cells: [
                  { value: "A-1", valueKind: "text", formula: null, deterministic: true },
                  { value: "一店", valueKind: "text", formula: null, deterministic: true },
                  { value: "10.25", valueKind: "number", formula: null, deterministic: true }
                ]
              },
              {
                sourceRowNumber: 3,
                sourceEndRowNumber: 3,
                dataRowNumber: 2,
                cells: [
                  { value: "A-2", valueKind: "text", formula: null, deterministic: true },
                  { value: "一店", valueKind: "text", formula: null, deterministic: true },
                  { value: "20.00", valueKind: "number", formula: null, deterministic: true }
                ]
              }
            ]
          }
        },
        context: {
          storeName: "一店",
          period: "2026-03-01 至 2026-03-31",
          businessTitle: "有一笔金额没有对应上",
          whatHappened: "订单中有金额，但平台记录中没有找到对应项。",
          whatItAffects: "本月差额为 ¥20.0000。",
          suggestedAction: "核对黄色原始记录是否属于本月。"
        },
        comparison: {
          businessKey: "A-2",
          expectedAmount: "20.0000",
          actualAmount: "0.0000",
          matchedAmount: "0.0000",
          differenceAmount: "20.0000"
        },
        trace: [
          { label: "保存原文件", detail: "按内容保存。" },
          { label: "等待业务确认", detail: "不会改写金额。" }
        ],
        sourceField: "金额",
        sourceValue: "20.00"
      }),
      { headers: { "Content-Type": "application/json" } }
    );
  });

  render(<EvidenceWorkbench onClose={vi.fn()} unresolvedId="review-1" />);

  expect(
    await screen.findByRole("heading", { name: "有一笔金额没有对应上" })
  ).toBeInTheDocument();
  expect(
    await screen.findByText("只读表格：三月订单.csv，第 3 行")
  ).toBeInTheDocument();
  expect(screen.getByText("核对黄色原始记录是否属于本月。"))
    .toBeInTheDocument();
  expect(screen.getByRole("link", { name: "下载这份原文件" }))
    .toHaveAttribute(
      "href",
      "/api/v1/reviews/review-1/evidence/snapshot-1/original"
    );
});

test("旧结果只有推测线索时不会伪装成精准原始行", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        unresolvedId: "review-legacy",
        balanceId: "balance-legacy",
        lineageStatus: "legacy_inferred",
        sources: [
          {
            snapshotId: "snapshot-legacy",
            artifactId: null,
            originalName: "历史流水.xlsx",
            sourceMember: null,
            sourceSheet: null,
            rowNumber: 88,
            field: null,
            normalizedValue: null,
            normalizationVersion: null,
            ruleVersionId: null,
            sourceKind: null
          }
        ]
      }),
      { headers: { "Content-Type": "application/json" } }
    )
  );

  render(
    <EvidenceWorkbench onClose={vi.fn()} unresolvedId="review-legacy" />
  );

  expect(
    await screen.findByText("这条旧结果只有文件线索，不能声称已经精准定位")
  ).toBeInTheDocument();
  expect(screen.getByText("旧记录曾指向第 88 行", { exact: false }))
    .toBeInTheDocument();
  expect(screen.queryByText(/页面已定位/)).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "下载这份原文件" }))
    .not.toBeInTheDocument();
  expect(fetchSpy).toHaveBeenCalledTimes(1);
});
