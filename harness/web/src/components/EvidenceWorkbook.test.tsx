import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import EvidenceWorkbook, {
  buildEvidenceWorkbookSnapshot,
  type EvidenceWorkbookProps
} from "./EvidenceWorkbook";

afterEach(cleanup);

const windowData: EvidenceWorkbookProps = {
  sheetName: "账务明细",
  columns: ["订单号", "平台净额", "交易时间"],
  rows: [
    ["A-100", "900.0000", "2026-06-01 10:00:00"],
    ["A-101", "-104739.6900", "2026-06-02 11:30:00"],
    ["A-102", true, null]
  ],
  startRow: 9889,
  targetRow: 9890,
  targetColumnIndex: 1,
  originalName: "202606_账务明细.xlsx"
};

describe("buildEvidenceWorkbookSnapshot", () => {
  test("保留原始行号、精确金额字符串并高亮目标单元格", () => {
    const snapshot = buildEvidenceWorkbookSnapshot(windowData);
    const sheet = snapshot.sheets["evidence-sheet"];

    expect(sheet.name).toBe("账务明细");
    expect(sheet.cellData?.[0]?.[0]?.v).toBe("原始行");
    expect(sheet.cellData?.[1]?.[0]?.v).toBe(9889);
    expect(sheet.cellData?.[2]?.[0]?.v).toBe(9890);
    expect(sheet.cellData?.[2]?.[2]).toMatchObject({
      v: "-104739.6900",
      s: "evidence-target"
    });
    expect(snapshot.custom?.evidence).toMatchObject({
      sourceStartRow: 9889,
      sourceTargetRow: 9890,
      sourceTargetColumnIndex: 1,
      targetA1Notation: "C3"
    });
  });

  test("目标行或列越过有限窗口时拒绝生成误导定位", () => {
    expect(() =>
      buildEvidenceWorkbookSnapshot({
        ...windowData,
        targetRow: 9999
      })
    ).toThrow("目标行不在当前证据窗口中");
    expect(() =>
      buildEvidenceWorkbookSnapshot({
        ...windowData,
        targetColumnIndex: 9
      })
    ).toThrow("目标列不在当前证据窗口中");
  });

  test("较远目标使用工作簿初始滚动位置进入可视区域", () => {
    const snapshot = buildEvidenceWorkbookSnapshot({
      ...windowData,
      rows: Array.from({ length: 20 }, (_, index) => [
        `A-${index}`,
        `${index}.0000`,
        "2026-06-01"
      ]),
      startRow: 100,
      targetRow: 115,
      targetColumnIndex: 2
    });
    const sheet = snapshot.sheets["evidence-sheet"];

    expect(sheet.scrollTop).toBe(364);
    expect(sheet.scrollLeft).toBe(168);
    expect(sheet.cellData?.[16]?.[3]?.s).toBe("evidence-target");
  });

  test("使用真实源行号定位多行记录且无目标字段时不误高亮", () => {
    const snapshot = buildEvidenceWorkbookSnapshot({
      ...windowData,
      sourceRowNumbers: [9889, 9891, 9894],
      targetRow: 9891,
      targetColumnIndex: null
    });
    const sheet = snapshot.sheets["evidence-sheet"];

    expect(sheet.cellData?.[2]?.[0]?.v).toBe(9891);
    expect(sheet.cellData?.[2]?.[1]?.s).not.toBe("evidence-target");
    expect(snapshot.custom?.evidence).toMatchObject({
      sourceTargetRow: 9891,
      sourceTargetColumnIndex: null,
      targetA1Notation: "A3"
    });
  });
});

test("无窗口数据时展示可访问降级状态且不初始化 Univer", () => {
  render(
    <EvidenceWorkbook
      {...windowData}
      columns={[]}
      rows={[]}
      targetColumnIndex={0}
    />
  );

  expect(
    screen.getByRole("heading", { name: "原始表格定位" })
  ).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(
    "当前证据没有可展示的表格行。"
  );
});
