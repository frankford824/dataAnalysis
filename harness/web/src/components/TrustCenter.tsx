import { useEffect, useMemo, useState } from "react";

import { loadTrustMatrix } from "../api";
import type {
  TrustCell,
  TrustCellStatus,
  TrustCheck,
  TrustMatrix
} from "../types";

const STATUS_ORDER: TrustCellStatus[] = [
  "amount_mismatch",
  "missing_sources",
  "waiting_review",
  "processing",
  "collecting",
  "usable"
];

function periodLabel(value: string): string {
  const match = value.match(/^(\d{4})-(\d{2})$/);
  return match ? `${match[1]}年${Number(match[2])}月` : value;
}

function platformLabel(value: string): string {
  const labels: Record<string, string> = {
    "1688": "1688",
    douyin: "抖音电商",
    jd: "京东",
    pinduoduo: "拼多多",
    taobao: "淘宝 / 天猫",
    wechat: "微信支付"
  };
  return labels[value.toLowerCase()] ?? value;
}

function checkStateLabel(state: TrustCheck["state"]): string {
  if (state === "passed") return "已通过";
  if (state === "failed") return "需要处理";
  if (state === "pending") return "等待完成";
  return "本月不需要";
}

function actionLabel(cell: TrustCell): string {
  if (cell.status === "amount_mismatch" && cell.firstReviewId) {
    return "查看这笔原始记录";
  }
  if (cell.status === "missing_sources") return "查看还缺什么";
  if (cell.status === "waiting_review") return "去完成确认";
  if (cell.status === "processing") return "查看整理进度";
  if (cell.status === "collecting") return "查看本月进度";
  return "查看本月经营数据";
}

function compareCells(left: TrustCell, right: TrustCell): number {
  const statusDifference =
    STATUS_ORDER.indexOf(left.status) - STATUS_ORDER.indexOf(right.status);
  if (statusDifference) return statusDifference;
  const amountDifference =
    Math.abs(Number(right.facts.unresolvedAmount)) -
    Math.abs(Number(left.facts.unresolvedAmount));
  if (amountDifference) return amountDifference;
  return left.storeName.localeCompare(right.storeName, "zh-CN");
}

function TrustDetail({
  cell,
  onClose,
  onAction
}: {
  cell: TrustCell;
  onClose?: () => void;
  onAction: (cell: TrustCell) => void;
}) {
  return (
    <aside
      aria-label={`${cell.storeName}${periodLabel(cell.period)}核验说明`}
      className={`trust-detail trust-detail--${cell.status}`}
    >
      <header>
        <div>
          <strong>{cell.statusLabel}</strong>
          <span>
            {cell.storeName} · {periodLabel(cell.period)}
          </span>
        </div>
        {onClose ? (
          <button
            aria-label="关闭店铺月份说明"
            className="trust-detail-close"
            onClick={onClose}
            type="button"
          >
            关闭
          </button>
        ) : null}
      </header>

      <div className="trust-copy">
        <section>
          <h3>发生了什么</h3>
          <p>{cell.explanation.happened}</p>
        </section>
        <section>
          <h3>会影响什么</h3>
          <p>{cell.explanation.impact}</p>
        </section>
        <section>
          <h3>建议怎么做</h3>
          <p>{cell.explanation.action}</p>
        </section>
      </div>

      <button className="trust-primary-action" onClick={() => onAction(cell)} type="button">
        {actionLabel(cell)}
      </button>

      <details className="trust-checks">
        <summary>系统检查了哪些事实</summary>
        <ul>
          {cell.checks.map((check) => (
            <li key={check.key}>
              <span className={`trust-check-state trust-check-state--${check.state}`}>
                {checkStateLabel(check.state)}
              </span>
              <div>
                <strong>{check.label}</strong>
                <p>{check.explanation}</p>
              </div>
            </li>
          ))}
        </ul>
      </details>

      <footer>
        <strong>确认后会怎样</strong>
        <p>{cell.explanation.outcome}</p>
        {cell.facts.lastCalculatedAt ? (
          <small>
            最近整理：{new Date(cell.facts.lastCalculatedAt).toLocaleString("zh-CN")}
          </small>
        ) : null}
      </footer>
    </aside>
  );
}

export function TrustCenter({
  refreshVersion,
  onOpenEvidence,
  onOpenProgress,
  onOpenReviews,
  onOpenAnalytics,
  onScopeChange,
  onOpenCatalog,
  onOpenBalances,
  initialPeriod,
  initialStoreId
}: {
  refreshVersion: number;
  onOpenEvidence: (reviewId: string) => void;
  onOpenProgress: () => void;
  onOpenReviews: () => void;
  onOpenAnalytics: () => void;
  onScopeChange?: (cell: TrustCell) => void;
  onOpenCatalog?: () => void;
  onOpenBalances?: () => void;
  initialPeriod?: string | null;
  initialStoreId?: string | null;
}) {
  const [matrix, setMatrix] = useState<TrustMatrix | null>(null);
  const [selectedPeriod, setSelectedPeriod] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    loadTrustMatrix(controller.signal)
      .then((result) => {
        if (
          !Array.isArray(result.cells) ||
          !Array.isArray(result.periods) ||
          !Array.isArray(result.stores) ||
          !result.summary
        ) {
          throw new Error("服务尚未返回店铺月份核验结果");
        }
        setMatrix(result);
        setSelectedPeriod((current) =>
          current && result.periods.includes(current)
            ? current
            : initialPeriod && result.periods.includes(initialPeriod)
              ? initialPeriod
            : result.currentPeriod
        );
        setSelectedKey((current) => {
          if (
            current &&
            result.cells.some(
              (cell) => `${cell.storeId}:${cell.period}` === current
            )
          ) {
            return current;
          }
          const initial =
            result.cells.find(
              (cell) =>
                cell.storeId === initialStoreId &&
                cell.period ===
                  (initialPeriod && result.periods.includes(initialPeriod)
                    ? initialPeriod
                    : result.currentPeriod)
            ) ??
            result.firstAttention ??
            result.cells.find((cell) => cell.period === result.currentPeriod) ??
            result.cells[0];
          return initial ? `${initial.storeId}:${initial.period}` : null;
        });
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法读取本月核验状态");
      });
    return () => controller.abort();
  }, [initialPeriod, initialStoreId, refreshVersion]);

  const cellsByKey = useMemo(
    () =>
      new Map(
        (matrix?.cells ?? []).map((cell) => [
          `${cell.storeId}:${cell.period}`,
          cell
        ])
      ),
    [matrix]
  );
  const selectedCell =
    (selectedKey ? cellsByKey.get(selectedKey) : undefined) ??
    matrix?.firstAttention ??
    matrix?.cells[0] ??
    null;
  const currentCells = useMemo(
    () =>
      (matrix?.cells ?? [])
        .filter((cell) => cell.period === selectedPeriod)
        .slice()
        .sort(compareCells),
    [matrix, selectedPeriod]
  );
  const selectedSummary = useMemo(
    () => ({
      usable: currentCells.filter((cell) => cell.status === "usable").length,
      missing: currentCells.filter(
        (cell) => cell.status === "missing_sources"
      ).length,
      mismatch: currentCells.filter(
        (cell) => cell.status === "amount_mismatch"
      ).length,
      waiting: currentCells.filter((cell) =>
        ["waiting_review", "processing", "collecting"].includes(cell.status)
      ).length
    }),
    [currentCells]
  );
  const selectedVerdict = useMemo(() => {
    if (!selectedPeriod || selectedPeriod === matrix?.currentPeriod) {
      return matrix?.summary.verdict ?? "正在检查每家店的本月数据。";
    }
    const attention =
      selectedSummary.missing +
      selectedSummary.mismatch +
      currentCells.filter((cell) => cell.status === "waiting_review").length;
    if (!currentCells.length) return "这个月份还没有形成可核验的店铺数据。";
    if (selectedSummary.usable === currentCells.length) {
      return `${periodLabel(selectedPeriod)}的店铺数据均可使用。`;
    }
    return `${periodLabel(selectedPeriod)}有 ${attention} 家店需要处理，${selectedSummary.usable} 家可以使用。`;
  }, [currentCells, matrix?.currentPeriod, matrix?.summary.verdict, selectedPeriod, selectedSummary]);

  const runAction = (cell: TrustCell) => {
    onScopeChange?.(cell);
    if (cell.status === "amount_mismatch" && cell.firstReviewId) {
      onOpenEvidence(cell.firstReviewId);
      return;
    }
    if (
      cell.status === "missing_sources" ||
      cell.status === "processing" ||
      cell.status === "collecting"
    ) {
      onOpenProgress();
      return;
    }
    if (cell.status === "waiting_review") {
      onOpenReviews();
      return;
    }
    onOpenAnalytics();
  };

  return (
    <section aria-labelledby="trust-heading" className="trust-center">
      <header className="trust-heading">
        <div>
          <h2 id="trust-heading">这个月的数据能不能信？</h2>
          <p>{selectedVerdict}</p>
        </div>
        {matrix?.currentPeriod ? (
          <label>
            查看月份
            <select
              onChange={(event) => {
                const period = event.target.value;
                setSelectedPeriod(period);
                const first = matrix.cells
                  .filter((cell) => cell.period === period)
                  .sort(compareCells)[0];
                setSelectedKey(first ? `${first.storeId}:${first.period}` : null);
                if (first) onScopeChange?.(first);
                setMobileDetailOpen(false);
              }}
              value={selectedPeriod ?? matrix.currentPeriod}
            >
              {matrix.periods.slice().reverse().map((period) => (
                <option key={period} value={period}>
                  {periodLabel(period)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <details className="trust-more-actions">
          <summary>更多核验信息</summary>
          <div>
            <button onClick={onOpenBalances} type="button">
              核对明细
            </button>
            <button onClick={onOpenCatalog} type="button">
              数据覆盖
            </button>
            <button onClick={onOpenProgress} type="button">
              实时进度
            </button>
          </div>
        </details>
      </header>

      {error ? (
        <div className="trust-error" role="alert">
          <strong>暂时无法判断本月数据</strong>
          <p>{error}</p>
        </div>
      ) : null}

      {!matrix && !error ? (
        <div aria-label="正在检查店铺月份" className="trust-skeleton" />
      ) : null}

      {matrix ? (
        <>
          <div aria-label="当前月份核验概况" className="trust-summary">
            <button
              className="trust-summary-main"
              disabled={selectedSummary.usable === 0}
              onClick={() => {
                const firstUsable = currentCells.find(
                  (cell) => cell.status === "usable"
                );
                if (firstUsable) {
                  setSelectedKey(`${firstUsable.storeId}:${firstUsable.period}`);
                  onScopeChange?.(firstUsable);
                }
              }}
              type="button"
            >
              <strong>{selectedSummary.usable}</strong>
              <span>家可以直接看</span>
            </button>
            <button
              disabled={selectedSummary.missing === 0}
              onClick={() => {
                const first = currentCells.find(
                  (cell) => cell.status === "missing_sources"
                );
                if (first) {
                  setSelectedKey(`${first.storeId}:${first.period}`);
                  onScopeChange?.(first);
                }
              }}
              type="button"
            >
              <strong>{selectedSummary.missing}</strong>
              <span>家还差文件</span>
            </button>
            <button
              disabled={selectedSummary.mismatch === 0}
              onClick={() => {
                const first = currentCells.find(
                  (cell) => cell.status === "amount_mismatch"
                );
                if (first) {
                  setSelectedKey(`${first.storeId}:${first.period}`);
                  onScopeChange?.(first);
                }
              }}
              type="button"
            >
              <strong>{selectedSummary.mismatch}</strong>
              <span>家金额要核对</span>
            </button>
            <button
              disabled={selectedSummary.waiting === 0}
              onClick={() => {
                const first = currentCells.find(
                  (cell) =>
                    cell.status === "waiting_review" ||
                    cell.status === "processing" ||
                    cell.status === "collecting"
                );
                if (first) {
                  setSelectedKey(`${first.storeId}:${first.period}`);
                  onScopeChange?.(first);
                }
              }}
              type="button"
            >
              <strong>
                {selectedSummary.waiting}
              </strong>
              <span>家等待完成</span>
            </button>
          </div>

          <div className="trust-layout">
            <div aria-label="当前月份店铺核验列表" className="trust-mobile-list">
              {currentCells.map((cell) => (
                <button
                  className={`trust-mobile-row trust-mobile-row--${cell.status}`}
                  key={`${cell.storeId}:${cell.period}`}
                  onClick={() => {
                    setSelectedKey(`${cell.storeId}:${cell.period}`);
                    onScopeChange?.(cell);
                    setMobileDetailOpen(true);
                  }}
                  type="button"
                >
                  <span>
                    <strong>{cell.storeName}</strong>
                    <small>{cell.explanation.happened}</small>
                  </span>
                  <b>{cell.statusLabel}</b>
                </button>
              ))}
            </div>
            <div className="trust-matrix" role="region" aria-label="店铺月份核验表">
              <table>
                <thead>
                  <tr>
                    <th scope="col">店铺</th>
                    {matrix.periods.map((period) => (
                      <th key={period} scope="col">
                        {periodLabel(period)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {matrix.stores.map((store) => (
                    <tr key={store.id}>
                      <th scope="row">
                        <strong>{store.name}</strong>
                        <small>{platformLabel(store.platformId)}</small>
                      </th>
                      {matrix.periods.map((period) => {
                        const cell = cellsByKey.get(`${store.id}:${period}`);
                        const key = `${store.id}:${period}`;
                        return (
                          <td key={period}>
                            {cell ? (
                              <button
                                aria-current={selectedKey === key ? "true" : undefined}
                                className={`trust-cell trust-cell--${cell.status}`}
                                onClick={() => {
                                  setSelectedPeriod(period);
                                  setSelectedKey(key);
                                  onScopeChange?.(cell);
                                  setMobileDetailOpen(true);
                                }}
                                type="button"
                              >
                                {cell.statusLabel}
                              </button>
                            ) : (
                              <span className="trust-cell-empty">尚无记录</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {selectedCell ? (
              <div
                className={`trust-detail-host ${
                  mobileDetailOpen ? "trust-detail-host--open" : ""
                }`}
              >
                <TrustDetail
                  cell={selectedCell}
                  onAction={runAction}
                  onClose={() => setMobileDetailOpen(false)}
                />
              </div>
            ) : (
              <div className="trust-empty">
                <strong>还没有形成可核验的店铺月份</strong>
                <p>系统会在原始文件处理完成后自动更新这里。</p>
              </div>
            )}
          </div>
          <p className="trust-boundary">{matrix.boundary}</p>
        </>
      ) : null}
    </section>
  );
}
