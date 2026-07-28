import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";

import {
  loadReviewEvidence,
  loadReviewEvidencePreview,
  reviewEvidenceOriginalUrl
} from "../api";
import type {
  EvidenceSourceLine,
  ReviewEvidenceDetail,
  ReviewEvidencePreview
} from "../types";

const EvidenceWorkbook = lazy(() => import("./EvidenceWorkbook"));

type WorkbenchTab = "source" | "comparison" | "trace";

function money(value: string): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4
  }).format(parsed);
}

function sourceLocation(source: EvidenceSourceLine): string {
  const parts = [source.sourceMember, source.sourceSheet]
    .filter((item): item is string => Boolean(item))
    .map((item) => item.trim());
  parts.push(`第 ${source.rowNumber.toLocaleString("zh-CN")} 行`);
  return parts.join(" · ");
}

export function EvidenceWorkbench({
  unresolvedId,
  onClose
}: {
  unresolvedId: string;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [detail, setDetail] = useState<ReviewEvidenceDetail | null>(null);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<string | null>(
    null
  );
  const [preview, setPreview] = useState<ReviewEvidencePreview | null>(null);
  const [tab, setTab] = useState<WorkbenchTab>("source");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setDetail(null);
    setPreview(null);
    loadReviewEvidence(unresolvedId, controller.signal)
      .then((result) => {
        setDetail(result);
        setSelectedSnapshotId(
          result.lineageStatus === "frozen"
            ? (result.sources[0]?.snapshotId ?? null)
            : null
        );
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法定位原始记录");
      });
    return () => controller.abort();
  }, [unresolvedId]);

  useEffect(() => {
    if (!selectedSnapshotId) return;
    const controller = new AbortController();
    setError(null);
    setPreview(null);
    loadReviewEvidencePreview(
      unresolvedId,
      selectedSnapshotId,
      controller.signal
    )
      .then(setPreview)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法打开原始记录");
      });
    return () => controller.abort();
  }, [selectedSnapshotId, unresolvedId]);

  const selectedSource = useMemo(
    () =>
      detail?.sources.find(
        (source) => source.snapshotId === selectedSnapshotId
      ) ?? null,
    [detail, selectedSnapshotId]
  );
  const rows = useMemo(
    () =>
      preview?.sheet.window.rows.map((row) =>
        row.cells.map((cell) => cell.formula ?? cell.value)
      ) ?? [],
    [preview]
  );

  return (
    <div
      className="evidence-workbench-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section
        aria-labelledby="evidence-workbench-title"
        aria-modal="true"
        className="evidence-workbench"
        role="dialog"
      >
        <header className="evidence-workbench-header">
          <div>
            <p className="eyebrow">原始记录核验</p>
            <h2 id="evidence-workbench-title">
              {preview?.context.businessTitle ?? "正在定位这笔疑问"}
            </h2>
            {preview ? (
              <p>
                {preview.context.storeName} · {preview.context.period} ·{" "}
                {preview.originalName}
              </p>
            ) : null}
          </div>
          <button
            aria-label="关闭原始记录核验"
            className="evidence-close"
            onClick={onClose}
            ref={closeRef}
            type="button"
          >
            关闭
          </button>
        </header>

        {detail?.lineageStatus === "frozen" && detail.sources.length > 1 ? (
          <label className="evidence-source-picker">
            查看哪份原始文件
            <select
              onChange={(event) => setSelectedSnapshotId(event.target.value)}
              value={selectedSnapshotId ?? ""}
            >
              {detail.sources.map((source) => (
                <option key={source.snapshotId} value={source.snapshotId}>
                  {source.originalName} · {sourceLocation(source)}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        {error ? (
          <div className="evidence-error" role="alert">
            <strong>这条记录暂时无法在页面中打开</strong>
            <p>{error}</p>
          </div>
        ) : null}

        {detail?.lineageStatus === "legacy_inferred" ? (
          <div className="evidence-warning" role="status">
            <strong>这条旧结果只有文件线索，不能声称已经精准定位</strong>
            <p>
              旧流程没有冻结可验证的文件、工作表和行绑定。系统不会据此自动确认，
              也不会把推测位置显示成原始证据。
            </p>
            {detail.sources.length ? (
              <ul>
                {detail.sources.map((source) => (
                  <li key={`${source.snapshotId}-${source.rowNumber}`}>
                    {source.originalName}：旧记录曾指向第{" "}
                    {source.rowNumber.toLocaleString("zh-CN")} 行，请人工从原文件重新核对。
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        {detail?.lineageStatus === "unavailable" ? (
          <div className="evidence-warning" role="status">
            <strong>当前没有足够依据定位原始行</strong>
            <p>
              系统已保留这笔疑问并阻止自动确认。重新计算并形成冻结证据后，
              才会开放表格定位。
            </p>
          </div>
        ) : null}

        {!detail && !error ? (
          <div className="evidence-loading" role="status">
            正在检查这笔疑问的证据状态…
          </div>
        ) : null}

        {detail?.lineageStatus === "frozen" &&
        selectedSnapshotId &&
        !preview &&
        !error ? (
          <div className="evidence-loading" role="status">
            正在从原始文件存档里定位这一行…
          </div>
        ) : null}

        {preview ? (
          <>
            <nav aria-label="原始记录核验内容" className="evidence-tabs">
              {(
                [
                  ["source", "原始表格"],
                  ["comparison", "金额为什么不一致"],
                  ["trace", "系统做了什么"]
                ] as const
              ).map(([value, label]) => (
                <button
                  aria-current={tab === value ? "page" : undefined}
                  className={tab === value ? "active" : ""}
                  key={value}
                  onClick={() => setTab(value)}
                  type="button"
                >
                  {label}
                </button>
              ))}
            </nav>

            <div className="evidence-workbench-body">
              {tab === "source" ? (
                <div className="evidence-source-layout">
                  <div className="evidence-sheet-stage">
                    <Suspense
                      fallback={
                        <div className="evidence-loading" role="status">
                          正在准备只读表格…
                        </div>
                      }
                    >
                      <EvidenceWorkbook
                        columns={preview.sheet.window.columns.map(
                          (column) => column.label
                        )}
                        originalName={preview.originalName}
                        rows={rows}
                        sheetName={preview.sheet.name}
                        sourceRowNumbers={preview.sheet.window.rows.map(
                          (row) => row.sourceRowNumber
                        )}
                        startRow={preview.sheet.window.startRowNumber}
                        targetColumnIndex={preview.sheet.window.targetColumnIndex}
                        targetRow={preview.sheet.window.targetRowNumber}
                      />
                    </Suspense>
                  </div>
                  <aside className="evidence-business-guide">
                    <section>
                      <h3>发生了什么</h3>
                      <p>{preview.context.whatHappened}</p>
                    </section>
                    <section>
                      <h3>会影响什么</h3>
                      <p>{preview.context.whatItAffects}</p>
                    </section>
                    <section>
                      <h3>建议怎么做</h3>
                      <p>{preview.context.suggestedAction}</p>
                    </section>
                    <div className="evidence-locator">
                      <strong>页面已定位</strong>
                      <span>
                        {selectedSource
                          ? sourceLocation(selectedSource)
                          : `第 ${preview.sheet.window.targetRowNumber} 行`}
                      </span>
                      <small>
                        {preview.sheet.window.targetColumnIndex === null
                          ? "已定位到原始行；当前规则没有提供可安全对应的单一字段。这里只查看，不会改动原文件。"
                          : "黄色位置是本条疑问的原始记录；这里只查看，不会改动原文件。"}
                      </small>
                    </div>
                    <a
                      className="secondary-button"
                      href={reviewEvidenceOriginalUrl(
                        unresolvedId,
                        preview.snapshotId
                      )}
                    >
                      下载这份原文件
                    </a>
                  </aside>
                </div>
              ) : null}

              {tab === "comparison" ? (
                <div className="evidence-explanation-panel">
                  <header>
                    <p className="eyebrow">当前差额</p>
                    <strong>
                      ¥ {money(preview.comparison.differenceAmount)}
                    </strong>
                  </header>
                  <dl>
                    <div>
                      <dt>应该对应的金额</dt>
                      <dd>¥ {money(preview.comparison.expectedAmount)}</dd>
                    </div>
                    <div>
                      <dt>目前找到的金额</dt>
                      <dd>¥ {money(preview.comparison.actualAmount)}</dd>
                    </div>
                    <div>
                      <dt>已经对应上的金额</dt>
                      <dd>¥ {money(preview.comparison.matchedAmount)}</dd>
                    </div>
                  </dl>
                  <p>{preview.context.whatHappened}</p>
                  <p>{preview.context.suggestedAction}</p>
                </div>
              ) : null}

              {tab === "trace" ? (
                <ol className="evidence-trace">
                  {preview.trace.map((step, index) => (
                    <li key={`${step.label}-${index}`}>
                      <span>{index + 1}</span>
                      <div>
                        <strong>{step.label}</strong>
                        <p>{step.detail}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : null}
            </div>
          </>
        ) : null}
      </section>
    </div>
  );
}
