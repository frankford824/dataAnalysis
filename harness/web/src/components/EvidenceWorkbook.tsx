import type { ICellData, IWorkbookData } from "@univerjs/core";
import type { FUniver } from "@univerjs/core/lib/facade";
import { useEffect, useId, useRef, useState } from "react";

export type EvidenceCellValue = string | number | boolean | null;

export interface EvidenceWorkbookProps {
  sheetName: string;
  columns: readonly string[];
  rows: ReadonlyArray<ReadonlyArray<EvidenceCellValue>>;
  startRow: number;
  sourceRowNumbers?: readonly number[];
  targetRow: number;
  targetColumnIndex: number | null;
  originalName: string;
}

export interface EvidenceWorkbookLocation {
  sheetRowIndex: number;
  sheetColumnIndex: number;
  a1Notation: string;
  hasTargetColumn: boolean;
}

const SHEET_ID = "evidence-sheet";
const HEADER_STYLE_ID = "evidence-header";
const ROW_NUMBER_STYLE_ID = "evidence-row-number";
const TARGET_STYLE_ID = "evidence-target";
const ORIGINAL_ROW_COLUMN = "原始行";

function columnName(columnIndex: number): string {
  let value = columnIndex + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function validateWindow({
  columns,
  rows,
  startRow,
  sourceRowNumbers,
  targetRow,
  targetColumnIndex
}: EvidenceWorkbookProps): EvidenceWorkbookLocation {
  if (!Number.isInteger(startRow) || startRow < 1) {
    throw new Error("原始窗口的起始行必须是大于 0 的整数。");
  }
  if (
    sourceRowNumbers &&
    (sourceRowNumbers.length !== rows.length ||
      sourceRowNumbers.some(
        (rowNumber) => !Number.isInteger(rowNumber) || rowNumber < 1
      ))
  ) {
    throw new Error("原始行号与证据窗口不一致。");
  }
  if (
    targetColumnIndex !== null &&
    (!Number.isInteger(targetColumnIndex) || targetColumnIndex < 0)
  ) {
    throw new Error("目标列位置无效。");
  }
  if (targetColumnIndex !== null && targetColumnIndex >= columns.length) {
    throw new Error("目标列不在当前证据窗口中。");
  }
  const targetRowOffset = sourceRowNumbers
    ? sourceRowNumbers.indexOf(targetRow)
    : targetRow - startRow;
  if (
    !Number.isInteger(targetRow) ||
    targetRowOffset < 0 ||
    targetRowOffset >= rows.length
  ) {
    throw new Error("目标行不在当前证据窗口中。");
  }

  // Row 0 contains field names and column 0 contains original source row numbers.
  const sheetRowIndex = targetRowOffset + 1;
  const hasTargetColumn = targetColumnIndex !== null;
  const sheetColumnIndex = hasTargetColumn ? targetColumnIndex + 1 : 0;
  return {
    sheetRowIndex,
    sheetColumnIndex,
    a1Notation: `${columnName(sheetColumnIndex)}${sheetRowIndex + 1}`,
    hasTargetColumn
  };
}

function cell(value: EvidenceCellValue, style?: string): ICellData {
  return {
    v: value ?? "",
    ...(style ? { s: style } : {})
  };
}

export function buildEvidenceWorkbookSnapshot(
  props: EvidenceWorkbookProps
): IWorkbookData {
  const location = validateWindow(props);
  const cellData: Record<number, Record<number, ICellData>> = {
    0: {
      0: cell(ORIGINAL_ROW_COLUMN, HEADER_STYLE_ID),
      ...Object.fromEntries(
        props.columns.map((column, index) => [
          index + 1,
          cell(column, HEADER_STYLE_ID)
        ])
      )
    }
  };

  props.rows.forEach((row, rowOffset) => {
    const sheetRowIndex = rowOffset + 1;
    const sourceRowNumber =
      props.sourceRowNumbers?.[rowOffset] ?? props.startRow + rowOffset;
    const cells: Record<number, ICellData> = {
      0: cell(sourceRowNumber, ROW_NUMBER_STYLE_ID)
    };
    props.columns.forEach((_, columnIndex) => {
      const style =
        location.hasTargetColumn &&
        sheetRowIndex === location.sheetRowIndex &&
        columnIndex + 1 === location.sheetColumnIndex
          ? TARGET_STYLE_ID
          : undefined;
      cells[columnIndex + 1] = cell(row[columnIndex] ?? null, style);
    });
    cellData[sheetRowIndex] = cells;
  });

  return {
    id: "evidence-workbook",
    name: props.originalName || "原始证据",
    appVersion: "0.25.1",
    locale: "zhCN" as IWorkbookData["locale"],
    styles: {
      [HEADER_STYLE_ID]: {
        bg: { rgb: "#EEF0F2" },
        bl: 1,
        cl: { rgb: "#202326" }
      },
      [ROW_NUMBER_STYLE_ID]: {
        bg: { rgb: "#F7F7F5" },
        cl: { rgb: "#62676D" }
      },
      [TARGET_STYLE_ID]: {
        bg: { rgb: "#FFE08A" },
        bl: 1,
        cl: { rgb: "#211A00" }
      }
    },
    sheetOrder: [SHEET_ID],
    sheets: {
      [SHEET_ID]: {
        id: SHEET_ID,
        name: props.sheetName || "原始数据",
        rowCount: props.rows.length + 1,
        columnCount: props.columns.length + 1,
        cellData,
        columnData: {
          0: { w: 86 },
          ...Object.fromEntries(
            props.columns.map((_, index) => [index + 1, { w: 168 }])
          )
        },
        freeze: {
          xSplit: 1,
          ySplit: 1,
          startRow: 1,
          startColumn: 1
        },
        scrollLeft: Math.max(0, (location.sheetColumnIndex - 2) * 168),
        scrollTop: Math.max(0, (location.sheetRowIndex - 3) * 28)
      }
    },
    custom: {
      evidence: {
        sourceStartRow: props.startRow,
        sourceTargetRow: props.targetRow,
        sourceTargetColumnIndex: props.targetColumnIndex,
        targetA1Notation: location.a1Notation
      }
    }
  };
}

type UniverRuntime = Awaited<ReturnType<typeof loadUniverRuntime>>;
let runtimePromise: Promise<{
  core: typeof import("@univerjs/core");
  facade: typeof import("@univerjs/core/lib/facade");
  UniverSheetsCorePreset: typeof import("@univerjs/preset-sheets-core").UniverSheetsCorePreset;
  zhCN: typeof import("@univerjs/preset-sheets-core/locales/zh-CN").default;
}> | null = null;

async function loadUniverRuntime() {
  runtimePromise ??= Promise.all([
    import("@univerjs/core"),
    import("@univerjs/core/lib/facade"),
    import("@univerjs/preset-sheets-core"),
    import("@univerjs/preset-sheets-core/locales/zh-CN"),
    import("@univerjs/preset-sheets-core/lib/index.css")
  ])
    .then(([core, facade, sheetsCore, zhCN]) => ({
      core,
      facade,
      UniverSheetsCorePreset: sheetsCore.UniverSheetsCorePreset,
      zhCN: zhCN.default
    }))
    .catch((cause: unknown) => {
      runtimePromise = null;
      throw cause;
    });
  return runtimePromise;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "表格查看器加载失败。";
}

export function EvidenceWorkbook(props: EvidenceWorkbookProps) {
  const titleId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const hasData = props.columns.length > 0 && props.rows.length > 0;

  useEffect(() => {
    if (!hasData) return;
    const container = containerRef.current;
    if (!container) return;

    // Univer mounts its own React root. A fresh child host avoids reusing that
    // third-party root during React StrictMode's development-only remount.
    const host = document.createElement("div");
    host.style.height = "100%";
    host.style.width = "100%";
    container.replaceChildren(host);

    let cancelled = false;
    let univerAPI: FUniver | null = null;
    let lifecycleDisposable: { dispose: () => void } | null = null;

    setStatus("loading");
    setError("");

    void (async () => {
      try {
        const snapshot = buildEvidenceWorkbookSnapshot(props);
        const location = validateWindow(props);
        const runtime = await loadUniverRuntime();
        if (cancelled) return;

        const univer = new runtime.core.Univer({
          locale: runtime.core.LocaleType.ZH_CN,
          locales: {
            [runtime.core.LocaleType.ZH_CN]: runtime.zhCN
          },
          logLevel: runtime.core.LogLevel.WARN
        });
        const preset = runtime.UniverSheetsCorePreset({
          container: host,
          contextMenu: false,
          footer: false,
          formulaBar: false,
          toolbar: false
        });
        preset.plugins.forEach((pluginDefinition) => {
          const [plugin, options] = Array.isArray(pluginDefinition)
            ? pluginDefinition
            : [pluginDefinition, undefined];
          univer.registerPlugin(plugin, options);
        });
        univerAPI = runtime.facade.FUniver.newAPI(univer);

        let positioned = false;
        lifecycleDisposable = univerAPI.addEvent(
          univerAPI.Event.LifeCycleChanged,
          ({ stage }) => {
            if (
              positioned ||
              stage !== univerAPI?.Enum.LifecycleStages.Rendered ||
              cancelled
            ) {
              return;
            }
            try {
              const activeWorkbook = univerAPI.getActiveWorkbook();
              if (!activeWorkbook) {
                throw new Error("表格尚未完成初始化。");
              }
              activeWorkbook.setEditable(false);
              const worksheet = activeWorkbook.getActiveSheet();
              const target = worksheet.getRange(
                location.sheetRowIndex,
                location.sheetColumnIndex
              );
              target.activate();
              positioned = true;
              lifecycleDisposable?.dispose();
              lifecycleDisposable = null;
              setStatus("ready");
            } catch (cause: unknown) {
              setError(errorMessage(cause));
              setStatus("error");
            }
          }
        );

        const workbook = univerAPI.createWorkbook(snapshot);
        workbook.setEditable(false);
      } catch (cause: unknown) {
        if (!cancelled) {
          setError(errorMessage(cause));
          setStatus("error");
        }
      }
    })();

    return () => {
      cancelled = true;
      lifecycleDisposable?.dispose();
      univerAPI?.dispose();
      host.remove();
    };
  }, [
    hasData,
    props.columns,
    props.originalName,
    props.rows,
    props.sheetName,
    props.sourceRowNumbers,
    props.startRow,
    props.targetColumnIndex,
    props.targetRow
  ]);

  if (!hasData) {
    return (
      <section aria-labelledby={titleId}>
        <h3 id={titleId}>原始表格定位</h3>
        <p role="status">当前证据没有可展示的表格行。</p>
      </section>
    );
  }

  return (
    <section aria-busy={status === "loading"} aria-labelledby={titleId}>
      <header
        style={{
          alignItems: "end",
          display: "flex",
          gap: "16px",
          justifyContent: "space-between",
          marginBottom: "12px"
        }}
      >
        <div>
          <h3 id={titleId} style={{ margin: 0 }}>
            原始表格定位
          </h3>
          <p style={{ margin: "6px 0 0" }}>
            {props.originalName || "未命名来源文件"} · {props.sheetName || "原始数据"} ·
            第 {props.targetRow.toLocaleString("zh-CN")} 行
          </p>
        </div>
        <strong aria-label="查看模式">只读</strong>
      </header>

      <div
        style={{
          border: "1px solid #D8DADD",
          borderRadius: "12px",
          height: "min(62vh, 620px)",
          minHeight: "360px",
          overflow: "hidden",
          position: "relative"
        }}
      >
        <div
          aria-label={`${props.originalName || "原始证据"}表格，只读`}
          data-testid="evidence-workbook-container"
          ref={containerRef}
          style={{ height: "100%", width: "100%" }}
          tabIndex={0}
        />
        {status === "loading" ? (
          <div
            role="status"
            style={{
              alignItems: "center",
              background: "#F7F7F5",
              display: "flex",
              inset: 0,
              justifyContent: "center",
              position: "absolute"
            }}
          >
            正在打开原始表格…
          </div>
        ) : null}
        {status === "error" ? (
          <div
            role="alert"
            style={{
              background: "#FFF7F5",
              color: "#8A241A",
              inset: 0,
              padding: "24px",
              position: "absolute"
            }}
          >
            <strong>暂时无法打开表格</strong>
            <p>{error}</p>
          </div>
        ) : null}
      </div>
      <p aria-live="polite" style={{ margin: "10px 0 0" }}>
        {status === "ready"
          ? props.targetColumnIndex === null
            ? "已定位到原始行；当前规则没有提供可安全对应的单一字段。"
            : "黄色单元格是本条疑问对应的原始位置。"
          : "表格加载完成后会自动定位到疑问位置。"}
      </p>
    </section>
  );
}

export default EvidenceWorkbook;
