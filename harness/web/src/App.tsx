import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode
} from "react";

import {
  applyLlmConfig,
  decideReview,
  disableLlm,
  discoverLlmModels,
  loadAnalyticsCatalog,
  loadAnalyticsOverview,
  loadCapabilities,
  loadComputeJobs,
  loadComputeTargets,
  loadDashboard,
  loadLlmConfig,
  loadProgress,
  loadReviewPage,
  runCompute,
  reviewExportUrl,
  suggestReviewExplanation,
  testLlmConnection
} from "./api";
import type {
  AnalyticsCatalog,
  AnalyticsMonthlyPnl,
  AnalyticsOverview,
  AnalyticsPlatform,
  AnalyticsQuery,
  AnalyticsStore,
  AnalyticsTransaction,
  BusinessDecision,
  CapabilityStatus,
  ComputeJob,
  ComputeTargetPlan,
  DashboardData,
  GateState,
  InputRevisionCandidate,
  InputRevisionGroup,
  LlmProtocol,
  LlmPublicConfig,
  ProgressData,
  ReviewGroup,
  ReviewItem,
  ReviewSuggestion
} from "./types";
import { EvidenceWorkbench } from "./components/EvidenceWorkbench";
import { PerformancePanel } from "./components/PerformancePanel";
import { TrustCenter } from "./components/TrustCenter";

type View =
  | "trust"
  | "analytics"
  | "catalog"
  | "progress"
  | "balances"
  | "reviews"
  | "models";
type PrimaryView = "trust" | "analytics" | "reviews" | "models";
type OverlayView = "catalog" | "progress" | "balances";

const VIEW_LABELS: Record<View, string> = {
  trust: "数据可信度",
  analytics: "经营看板",
  catalog: "数据目录",
  progress: "处理进度",
  balances: "核对结果",
  reviews: "待处理",
  models: "模型辅助"
};
const PRIMARY_VIEWS: PrimaryView[] = [
  "trust",
  "analytics",
  "reviews",
  "models"
];

const ANALYTICS_START_DATE = "2026-02-01";

function localIsoDate(date = new Date()): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

const CURRENT_DATE = localIsoDate();
const CURRENT_PERIOD = CURRENT_DATE.slice(0, 7);

export function displayPeriod(value: string, label?: string): string {
  let canonical = value;
  const compact = value.match(/^(\d{2})(\d{2})$/);
  const compactLong = value.match(/^(\d{4})(\d{2})$/);

  if (compact) {
    const month = Number(compact[2]);
    if (month >= 1 && month <= 12) {
      canonical = `20${compact[1]}-${compact[2]}`;
    }
  } else if (compactLong) {
    const month = Number(compactLong[2]);
    if (month >= 1 && month <= 12) {
      canonical = `${compactLong[1]}-${compactLong[2]}`;
    }
  }

  const match = canonical.match(/^(\d{4})-(\d{2})$/);
  const base =
    label ||
    (match
      ? `${match[1]}年${Number(match[2])}月`
      : value);
  return canonical === CURRENT_PERIOD ? `${base}（进行中）` : base;
}

function displayPeriodRange(periods: string[]): string {
  if (periods.length === 0) return "2026 年 2 月后暂无月份";
  const first = periods[0];
  const last = periods[periods.length - 1];
  return first === last
    ? displayPeriod(first)
    : `${displayPeriod(first)} 至 ${displayPeriod(last)}`;
}

function performancePeriod(value: string): string | undefined {
  const compact = value.match(/^(\d{2})(\d{2})$/);
  if (compact) return value;
  const canonical = value.match(/^20(\d{2})-(\d{2})$/);
  return canonical ? `${canonical[1]}${canonical[2]}` : undefined;
}

const PLATFORM_BUSINESS_NAMES: Record<string, string> = {
  "1688": "1688",
  douyin: "抖音电商",
  jd: "京东",
  pinduoduo: "拼多多",
  taobao: "淘宝 / 天猫",
  wechat: "微信支付"
};

function platformLabel(platform: AnalyticsPlatform): string {
  const id = platform.id.trim().toLowerCase();
  const name = platform.name.trim().toLowerCase();
  return (
    PLATFORM_BUSINESS_NAMES[id] ??
    PLATFORM_BUSINESS_NAMES[name] ??
    platform.name ??
    platform.id
  );
}

function storeLabel(
  store: AnalyticsStore,
  platforms: AnalyticsPlatform[]
): string {
  const platform = platforms.find((item) => item.id === store.platformId);
  const businessPlatform = platformLabel(
    platform ?? {
      id: store.platformId ?? store.platformName ?? "",
      name: store.platformName ?? store.platformId ?? "未分类"
    }
  );
  return `${store.name} · ${businessPlatform}`;
}

const GATE_LABELS: Record<GateState, string> = {
  complete: "已完成",
  active: "进行中",
  blocked: "需处理",
  pending: "未开始"
};

const BALANCE_STATUS_LABELS = {
  balanced: "已核清",
  partial: "部分核清",
  unresolved: "待核对"
} as const;

function formatCount(value: number): string {
  if (value > 9999) return `${Math.floor(value / 10000)}万+`;
  if (value > 999) return `${Math.floor(value / 1000)}千+`;
  return String(value);
}

function describeBalanceKey(value: string): {
  identifier: string;
  scope: string;
} {
  const separator = value.indexOf(":");
  const rawScope = separator === -1 ? value : value.slice(0, separator);
  const identifier = separator === -1 ? "未提供" : value.slice(separator + 1);
  const scope =
    rawScope === "order_platform"
      ? "订单与平台"
      : rawScope === "platform_cash"
        ? "平台与资金"
        : "其他核对";
  return { identifier, scope };
}

function formatMoney(value: string): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4
  }).format(parsed);
}

function formatAxisMoney(value: number): string {
  const absolute = Math.abs(value);
  if (absolute >= 10000) return `${(value / 10000).toFixed(1)}万`;
  if (absolute >= 1000) return `${(value / 1000).toFixed(1)}千`;
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: absolute < 10 ? 1 : 0
  }).format(value);
}

function formatTransactionTime(value: string | null): string {
  if (!value) return "时间未识别";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.replace("T", " ");
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(parsed);
}

function decimalToScaled(value: string): bigint | null {
  const match = value.trim().match(/^(-?)(\d+)(?:\.(\d+))?$/);
  if (!match) return null;
  const fraction = (match[3] ?? "").padEnd(4, "0").slice(0, 4);
  const scaled = BigInt(match[2]) * 10000n + BigInt(fraction);
  return match[1] === "-" ? -scaled : scaled;
}

function sumMoney(values: string[]): string | null {
  let total = 0n;
  for (const value of values) {
    const scaled = decimalToScaled(value);
    if (scaled === null) return null;
    total += scaled;
  }
  const sign = total < 0n ? "-" : "";
  const absolute = total < 0n ? -total : total;
  return `${sign}${absolute / 10000n}.${String(absolute % 10000n).padStart(4, "0")}`;
}

function pnlProfitValue(item: AnalyticsMonthlyPnl): string | null {
  if (item.operatingProfit) return item.operatingProfit;
  if (item.profit) return item.profit;
  const metrics = item.metrics ?? {};
  for (const key of [
    "operatingProfit",
    "operating_profit",
    "经营利润",
    "利润"
  ]) {
    if (metrics[key]) return metrics[key];
  }
  return null;
}

function certifiedProfit(data: AnalyticsOverview): string | null {
  if (data.coverage.profitStatus !== "system_checked") return null;
  const values = data.monthlyPnl
    .map(pnlProfitValue)
    .filter((value): value is string => value !== null);
  return values.length ? sumMoney(values) : null;
}

function formatDateTime(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(parsed);
}

function safeFileName(value: string): string {
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? "未命名文件";
}

function safeSourceLabel(value: string): string {
  if (!value || /[\\/]/.test(value) || /^[a-z]:/i.test(value)) {
    return "来源位置已隐藏";
  }
  return value;
}

function safeCandidateReason(value: string): string {
  if (
    !value ||
    /[\\/]/.test(value) ||
    /\b(?:artifact|revision|schema|parquet|etl|sha|digest)\b/i.test(value)
  ) {
    return "内容与同月另一版本不同，需要确认实际使用哪一份。";
  }
  return value;
}

function EmptyState({ children }: { children: string }) {
  return <div className="empty-state">{children}</div>;
}

function Modal({
  children,
  open,
  title,
  onClose,
  size = "default"
}: {
  children: ReactNode;
  open: boolean;
  title: string;
  onClose: () => void;
  size?: "default" | "wide";
}) {
  const panelRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    window.requestAnimationFrame(() => {
      panelRef.current?.querySelector<HTMLElement>(".modal-close")?.focus();
    });
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
      previousFocusRef.current?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;
  const titleId = `modal-${title.replace(/\s+/g, "-")}`;
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className={`modal-panel modal-panel--${size}`}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const focusable = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
            )
          ).filter((element) => !element.hasAttribute("hidden"));
          if (!focusable.length) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
        ref={panelRef}
        role="dialog"
      >
        <header className="modal-header">
          <h2 id={titleId}>{title}</h2>
          <button
            aria-label={`关闭${title}`}
            className="modal-close"
            onClick={onClose}
            type="button"
          >
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

function AnalyticsMetricCard({
  label,
  value,
  note,
  isMoney = true
}: {
  label: string;
  value: string | number;
  note: string;
  isMoney?: boolean;
}) {
  return (
    <div className="metric-card">
      <dt>{label}</dt>
      <dd>{isMoney ? `¥ ${formatMoney(String(value))}` : Number(value).toLocaleString("zh-CN")}</dd>
      <small>{note}</small>
    </div>
  );
}

function TrendChart({ points }: { points: AnalyticsOverview["trend"] }) {
  if (points.length === 0) {
    return (
      <div className="dashboard-empty">
        当前范围没有可绘制的每日数据。请调整月份、日期或店铺。
      </div>
    );
  }

  const width = 760;
  const height = 286;
  const margin = { top: 18, right: 18, bottom: 46, left: 68 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = points.flatMap((point) => [
    Number(point.netSales),
    Number(point.walletNet)
  ]);
  const finiteValues = values.filter(Number.isFinite);
  const minimum = Math.min(0, ...finiteValues);
  const maximum = Math.max(0, ...finiteValues);
  const span = maximum - minimum || 1;
  const x = (index: number) =>
    margin.left +
    (points.length === 1 ? plotWidth / 2 : (plotWidth * index) / (points.length - 1));
  const y = (value: string | number) =>
    margin.top + plotHeight - ((Number(value) - minimum) / span) * plotHeight;
  const path = (field: "netSales" | "walletNet") =>
    points
      .map((point, index) => `${index === 0 ? "M" : "L"} ${x(index)} ${y(point[field])}`)
      .join(" ");
  const tickCount = 4;
  const yTicks = Array.from(
    { length: tickCount + 1 },
    (_, index) => minimum + (span * index) / tickCount
  );
  const labelStep = Math.max(1, Math.ceil(points.length / 6));

  return (
    <div className="trend-chart">
      <div className="chart-legend" aria-hidden="true">
        <span><i className="legend-line legend-line--sales" />净销售额</span>
        <span><i className="legend-line legend-line--wallet" />平台钱包净额</span>
      </div>
      <svg
        aria-labelledby="trend-chart-title trend-chart-description"
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        <title id="trend-chart-title">每日经营趋势</title>
        <desc id="trend-chart-description">
          展示当前筛选范围内每日净销售额与平台钱包净额的变化。
        </desc>
        {yTicks.map((tick) => {
          const position = y(tick);
          return (
            <g key={tick}>
              <line
                className="chart-grid-line"
                x1={margin.left}
                x2={width - margin.right}
                y1={position}
                y2={position}
              />
              <text
                className="chart-axis-label"
                textAnchor="end"
                x={margin.left - 10}
                y={position + 4}
              >
                {formatAxisMoney(tick)}
              </text>
            </g>
          );
        })}
        <line
          className="chart-axis-line"
          x1={margin.left}
          x2={width - margin.right}
          y1={y(0)}
          y2={y(0)}
        />
        <path className="chart-line chart-line--sales" d={path("netSales")} />
        <path className="chart-line chart-line--wallet" d={path("walletNet")} />
        {points.map((point, index) => (
          <g key={`${point.date}-${index}`}>
            <circle
              className="chart-point chart-point--sales"
              cx={x(index)}
              cy={y(point.netSales)}
              r="3.5"
            />
            <circle
              className="chart-point chart-point--wallet"
              cx={x(index)}
              cy={y(point.walletNet)}
              r="3.5"
            />
            {index % labelStep === 0 || index === points.length - 1 ? (
              <text
                className="chart-axis-label"
                textAnchor="middle"
                x={x(index)}
                y={height - 17}
              >
                {point.date.slice(5)}
              </text>
            ) : null}
          </g>
        ))}
      </svg>
    </div>
  );
}

function StoreComparison({
  rows
}: {
  rows: AnalyticsOverview["storeBreakdown"];
}) {
  const largest = Math.max(
    1,
    ...rows.flatMap((row) => [
      Math.abs(Number(row.netSales)),
      Math.abs(Number(row.walletNet))
    ])
  );
  return (
    <div className="store-comparison">
      <div className="comparison-legend" aria-hidden="true">
        <span><i className="comparison-key comparison-key--sales" />净销售额</span>
        <span><i className="comparison-key comparison-key--wallet" />平台钱包净额</span>
      </div>
      <ul>
        {rows.map((row) => (
          <li key={row.storeId}>
            <strong>{row.storeName}</strong>
            <div className="comparison-measure">
              <span>净销售额</span>
              <div className="comparison-track">
                <i
                  className="comparison-bar comparison-bar--sales"
                  style={{ width: `${(Math.abs(Number(row.netSales)) / largest) * 100}%` }}
                />
              </div>
              <b>¥ {formatMoney(row.netSales)}</b>
            </div>
            <div className="comparison-measure">
              <span>钱包净额</span>
              <div className="comparison-track">
                <i
                  className="comparison-bar comparison-bar--wallet"
                  style={{ width: `${(Math.abs(Number(row.walletNet)) / largest) * 100}%` }}
                />
              </div>
              <b>¥ {formatMoney(row.walletNet)}</b>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function transactionDirection(item: AnalyticsTransaction): string {
  if (item.direction === "income") return "收入";
  if (item.direction === "expense") return "支出";
  return "金额为零";
}

function TransactionsView({
  transactions
}: {
  transactions: AnalyticsTransaction[];
}) {
  if (transactions.length === 0) {
    return <div className="dashboard-empty">当前范围没有交易记录。</div>;
  }
  return (
    <>
      <div className="transaction-table">
        <table>
          <thead>
            <tr>
              <th>交易时间</th>
              <th>店铺</th>
              <th>来源</th>
              <th>业务说明</th>
              <th>方向</th>
              <th className="numeric">金额</th>
            </tr>
          </thead>
          <tbody>
            {transactions.map((item, index) => (
              <tr key={`${item.storeId}-${item.occurredAt ?? "unknown"}-${index}`}>
                <td>{formatTransactionTime(item.occurredAt)}</td>
                <td>{item.storeName}</td>
                <td>{item.sourceLabel}</td>
                <td className="transaction-description">{item.businessDescription}</td>
                <td>{transactionDirection(item)}</td>
                <td className={`numeric amount amount--${item.direction}`}>
                  ¥ {formatMoney(item.amount)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="transaction-cards">
        {transactions.map((item, index) => (
          <li key={`${item.storeId}-${item.occurredAt ?? "unknown"}-${index}`}>
            <div>
              <time>{formatTransactionTime(item.occurredAt)}</time>
              <strong className={`amount amount--${item.direction}`}>
                ¥ {formatMoney(item.amount)}
              </strong>
            </div>
            <p>{item.businessDescription}</p>
            <small>{item.storeName} · {item.sourceLabel} · {transactionDirection(item)}</small>
          </li>
        ))}
      </ul>
    </>
  );
}

function CoveragePanel({
  catalog,
  catalogError,
  periodCount
}: {
  catalog: AnalyticsCatalog | null;
  catalogError: string | null;
  periodCount: number;
}) {
  return (
    <aside aria-labelledby="coverage-heading" className="coverage-panel">
      <div>
        <p className="eyebrow">当前数据覆盖</p>
        <h3 id="coverage-heading">哪些数据已进入经营汇总</h3>
      </div>
      {catalog ? (
        <>
          <dl>
            <div>
              <dt>发现店铺</dt>
              <dd>{catalog.discoveredStoreCount.toLocaleString("zh-CN")}</dd>
            </div>
            <div>
              <dt>已处理店铺</dt>
              <dd>{catalog.processedStoreCount.toLocaleString("zh-CN")}</dd>
            </div>
            <div>
              <dt>可用月份</dt>
              <dd>{periodCount.toLocaleString("zh-CN")}</dd>
            </div>
          </dl>
          <details className="discovered-scope">
            <summary>查看已发现的 {catalog.discoveredStoreCount} 个店铺</summary>
            <ul>
              {catalog.stores.map((store) => (
                <li key={store.id}>
                  <span>
                    <strong>{store.name}</strong>
                    <small>
                      {store.periods.length
                        ? `${store.periods[0]} 至 ${store.periods[store.periods.length - 1]}`
                        : "月份尚未识别"}
                    </small>
                  </span>
                  <em>{store.processed ? "已进入汇总" : "等待处理"}</em>
                </li>
              ))}
            </ul>
          </details>
          <p>
            {catalog.discoveredStoreCount > catalog.processedStoreCount
              ? `还有 ${catalog.discoveredStoreCount - catalog.processedStoreCount} 个已发现店铺尚未完成处理，因此不会进入当前金额汇总。下一步请先等待这些店铺的数据处理完成。`
              : "已发现的店铺都已完成处理；金额汇总仍以当前筛选和复核状态为准。"}
          </p>
        </>
      ) : catalogError ? (
        <p className="inline-error" role="status">{catalogError}</p>
      ) : (
        <div className="coverage-loading" aria-label="正在读取数据覆盖情况" />
      )}
    </aside>
  );
}

function CatalogView() {
  const [catalog, setCatalog] = useState<AnalyticsCatalog | null>(null);
  const [platformId, setPlatformId] = useState("all");
  const [period, setPeriod] = useState("all");
  const [storeQuery, setStoreQuery] = useState("");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    loadAnalyticsCatalog(controller.signal)
      .then(setCatalog)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法读取数据目录");
      });
    return () => controller.abort();
  }, []);

  const platforms = catalog?.platforms ?? [];
  const periods = Array.from(
    new Set(
      (catalog?.stores ?? []).flatMap((store) =>
        store.periods.filter(
          (item) => item >= ANALYTICS_START_DATE.slice(0, 7) && item <= CURRENT_PERIOD
        )
      )
    )
  ).sort((left, right) => right.localeCompare(left));
  const normalizedQuery = storeQuery.trim().toLocaleLowerCase("zh-CN");
  const stores = (catalog?.stores ?? []).filter((store) => {
    const matchesPlatform =
      platformId === "all" || store.platformId === platformId;
    const matchesPeriod = period === "all" || store.periods.includes(period);
    const matchesStore =
      !normalizedQuery ||
      store.name.toLocaleLowerCase("zh-CN").includes(normalizedQuery);
    return matchesPlatform && matchesPeriod && matchesStore;
  });

  return (
    <section aria-labelledby="catalog-heading" className="workspace-view">
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">真实服务目录</p>
          <h2 id="catalog-heading">平台、店铺与月份</h2>
          <p>只展示服务实际发现的数据；默认范围从 2026 年 2 月到当前月。</p>
        </div>
        <button
          disabled={!catalog}
          onClick={() => setDetailsOpen(true)}
          type="button"
        >
          筛选并查看全部
        </button>
      </header>

      {error ? (
        <div className="analytics-error" role="alert">
          <strong>数据目录暂时不可用</strong>
          <p>{error}</p>
        </div>
      ) : null}

      {!catalog && !error ? (
        <div className="dashboard-skeleton" aria-label="正在读取数据目录">
          <i /><i /><i />
        </div>
      ) : null}

      {catalog ? (
        <>
          <dl className="catalog-summary">
            <div>
              <dt>当前显示</dt>
              <dd>{stores.length.toLocaleString("zh-CN")}</dd>
              <small>家店铺</small>
            </div>
            <div>
              <dt>已进入汇总</dt>
              <dd>
                {stores.filter((store) => store.processed).length.toLocaleString("zh-CN")}
              </dd>
              <small>家店铺</small>
            </div>
            <div>
              <dt>可选择月份</dt>
              <dd>{periods.length.toLocaleString("zh-CN")}</dd>
              <small>从 2026 年 2 月开始</small>
            </div>
          </dl>

          {platforms.length === 0 ? (
            <p className="service-boundary" role="status">
              当前服务已返回店铺和月份，但尚未返回独立的平台字段；系统不会根据名称猜测平台。
            </p>
          ) : null}

          {catalog.stores.length ? (
            <div className="workspace-preview">
              <header>
                <h3>最近发现的店铺</h3>
                <span>共 {catalog.stores.length.toLocaleString("zh-CN")} 家</span>
              </header>
              <ul className="catalog-list catalog-list--preview">
              {catalog.stores.slice(0, 8).map((store) => {
                const visiblePeriods = store.periods.filter(
                  (item) =>
                    item >= ANALYTICS_START_DATE.slice(0, 7) &&
                    item <= CURRENT_PERIOD
                );
                return (
                  <li key={store.id}>
                    <div>
                      <strong>{store.name}</strong>
                      <span>
                        {store.platformName
                          ? store.platformName
                          : "平台待服务分类"}
                      </span>
                    </div>
                    <div>
                      <span>
                        {displayPeriodRange(visiblePeriods)}
                      </span>
                      <small>{store.fileCount.toLocaleString("zh-CN")} 份文件</small>
                    </div>
                    <em className={store.processed ? "catalog-ready" : "catalog-waiting"}>
                      {store.processed ? "已进入汇总" : "等待处理"}
                    </em>
                  </li>
                );
              })}
              </ul>
            </div>
          ) : (
            <div className="dashboard-empty">当前服务还没有发现店铺。</div>
          )}
        </>
      ) : null}

      <Modal
        onClose={() => setDetailsOpen(false)}
        open={detailsOpen}
        size="wide"
        title="筛选店铺与月份"
      >
        <form className="catalog-filters" onSubmit={(event) => event.preventDefault()}>
          <div className="field">
            <label htmlFor="catalog-platform">平台</label>
            <select
              disabled={platforms.length === 0}
              id="catalog-platform"
              onChange={(event) => setPlatformId(event.target.value)}
              value={platformId}
            >
              <option value="all">
                {platforms.length ? "所有平台" : "平台尚未由服务分类"}
              </option>
              {platforms.map((platform) => (
                <option key={platform.id} value={platform.id}>
                  {platformLabel(platform)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="catalog-store">店铺名称</label>
            <input
              id="catalog-store"
              onChange={(event) => setStoreQuery(event.target.value)}
              placeholder="输入店铺名称"
              type="search"
              value={storeQuery}
            />
          </div>
          <div className="field">
            <label htmlFor="catalog-period">月份</label>
            <select
              id="catalog-period"
              onChange={(event) => setPeriod(event.target.value)}
              value={period}
            >
              <option value="all">2026 年 2 月至当前月</option>
              {periods.map((item) => (
                <option key={item} value={item}>
                  {displayPeriod(item)}
                </option>
              ))}
            </select>
          </div>
        </form>
        {stores.length ? (
          <ul className="catalog-list">
            {stores.map((store) => {
              const visiblePeriods = store.periods.filter(
                (item) =>
                  item >= ANALYTICS_START_DATE.slice(0, 7) &&
                  item <= CURRENT_PERIOD
              );
              return (
                <li key={store.id}>
                  <div>
                    <strong>{store.name}</strong>
                    <span>{store.platformName ?? "平台待服务分类"}</span>
                  </div>
                  <div>
                    <span>{displayPeriodRange(visiblePeriods)}</span>
                    <small>{store.fileCount.toLocaleString("zh-CN")} 份文件</small>
                  </div>
                  <em className={store.processed ? "catalog-ready" : "catalog-waiting"}>
                    {store.processed ? "已进入汇总" : "等待处理"}
                  </em>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="dashboard-empty">当前筛选范围没有发现店铺。</div>
        )}
      </Modal>
    </section>
  );
}

const INITIAL_ANALYTICS_QUERY: AnalyticsQuery = {
  platformId: "all",
  storeId: "all",
  period: "all",
  fromDate: ANALYTICS_START_DATE,
  toDate: CURRENT_DATE,
  limit: 50
};

function AnalyticsView({
  data,
  initialScope
}: {
  data: DashboardData;
  initialScope?: { storeId: string | null; period: string | null };
}) {
  const scopedInitialQuery: AnalyticsQuery = {
    ...INITIAL_ANALYTICS_QUERY,
    storeId: initialScope?.storeId ?? "all",
    period: initialScope?.period ?? "all"
  };
  const [query, setQuery] = useState<AnalyticsQuery>(scopedInitialQuery);
  const [draftQuery, setDraftQuery] = useState<AnalyticsQuery>(
    scopedInitialQuery
  );
  const [filterOpen, setFilterOpen] = useState(false);
  const [detail, setDetail] = useState<
    "stores" | "transactions" | "coverage" | "performance" | null
  >(null);
  const [overview, setOverview] = useState<AnalyticsOverview | null>(null);
  const [catalog, setCatalog] = useState<AnalyticsCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const invalidDateRange =
    Boolean(query.fromDate && query.toDate) && query.fromDate > query.toDate;
  const platforms = overview?.filters.platforms ?? [];
  const stores = (overview?.filters.stores ?? []).filter(
    (store) =>
      draftQuery.platformId === "all" ||
      !store.platformId ||
      store.platformId === draftQuery.platformId
  );

  useEffect(() => {
    if (!initialScope?.storeId && !initialScope?.period) return;
    const next = {
      ...INITIAL_ANALYTICS_QUERY,
      storeId: initialScope.storeId ?? "all",
      period: initialScope.period ?? "all"
    };
    setQuery(next);
    setDraftQuery(next);
  }, [initialScope?.period, initialScope?.storeId]);

  useEffect(() => {
    const controller = new AbortController();
    loadAnalyticsCatalog(controller.signal)
      .then(setCatalog)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setCatalogError(
          cause instanceof Error ? cause.message : "无法读取数据覆盖情况"
        );
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (invalidDateRange) {
      setError("起始日期不能晚于结束日期。");
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadAnalyticsOverview(query, controller.signal)
      .then(setOverview)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法读取经营数据");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [invalidDateRange, query]);

  const updateDraftQuery = <Key extends keyof AnalyticsQuery>(
    key: Key,
    value: AnalyticsQuery[Key]
  ) => {
    setDraftQuery((current) => ({ ...current, [key]: value }));
  };
  const profit = overview ? certifiedProfit(overview) : null;
  const coverageStatus = overview?.coverage.status ?? "no_data";
  const multipleProcessedStores =
    (catalog?.processedStoreCount ?? 0) > 1 &&
    (overview?.storeBreakdown.length ?? 0) > 1;
  const pendingCount =
    data.progress.unresolvedCount +
    data.businessDecisions.filter((decision) => decision.status === "pending")
      .length +
    data.inputRevisionGroups.length;
  const platformName =
    query.platformId === "all"
      ? "所有平台"
      : platformLabel(
          platforms.find((platform) => platform.id === query.platformId) ?? {
            id: query.platformId,
            name: query.platformId
          }
        );
  const storeName =
    query.storeId === "all"
      ? "所有已处理店铺"
      : (() => {
          const selected = overview?.filters.stores.find(
            (store) => store.id === query.storeId
          );
          return selected ? storeLabel(selected, platforms) : "所选店铺";
        })();
  const periodName =
    query.period === "all" ? "全部可用月份" : displayPeriod(query.period);

  return (
    <section aria-labelledby="analytics-heading" className="workspace-view analytics-workspace">
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">真实处理结果</p>
          <h2 id="analytics-heading">经营看板</h2>
          <p>金额来自当前已处理数据，范围变更后即时重算。</p>
        </div>
        {overview ? (
          <span className={`coverage-status coverage-status--${coverageStatus}`}>
            {coverageStatus === "system_checked"
              ? "当前范围已通过系统检查"
              : coverageStatus === "review_required"
                ? "当前范围待复核"
                : "当前范围暂无结果"}
          </span>
        ) : null}
      </header>

      <div className="scope-bar">
        <div>
          <span>{platformName}</span>
          <span>{storeName}</span>
          <span>{periodName}</span>
          <small>{query.fromDate} 至 {query.toDate}</small>
        </div>
        <button
          className="secondary-button"
          disabled={!overview || loading}
          onClick={() => {
            setDraftQuery(query);
            setFilterOpen(true);
          }}
          type="button"
        >
          筛选范围
        </button>
      </div>

      {error ? (
        <div className="analytics-error" role="alert">
          <strong>当前经营数据未能显示</strong>
          <p>{error}</p>
        </div>
      ) : null}

      {!overview && loading ? (
        <div className="dashboard-skeleton" aria-label="正在读取经营数据">
          <i /><i /><i /><i />
        </div>
      ) : null}

      {overview ? (
        <>
          {overview.coverage.status === "no_data" ? (
            <div className="dashboard-empty">
              当前筛选范围还没有成功完成的处理结果。请调整店铺、月份或日期。
            </div>
          ) : (
            <>
              <dl className="metric-grid">
                <AnalyticsMetricCard
                  label="销售额"
                  note="订单原始销售金额，未扣除退款"
                  value={overview.metrics.orderGross}
                />
                <AnalyticsMetricCard
                  label="退款"
                  note="当前范围内已识别退款金额"
                  value={overview.metrics.refunds}
                />
                <AnalyticsMetricCard
                  label="净销售"
                  note="销售额减去退款"
                  value={overview.metrics.netSales}
                />
                <AnalyticsMetricCard
                  label="平台钱包净额"
                  note="支付宝、微信等平台收支净额"
                  value={overview.metrics.walletNet}
                />
                <AnalyticsMetricCard
                  isMoney={false}
                  label="订单数"
                  note="按订单业务编号去重"
                  value={overview.metrics.orderCount}
                />
              </dl>

              <div className="analytics-main-grid">
                <section className="dashboard-section trend-panel" aria-labelledby="trend-heading">
                  <header>
                    <div>
                      <p className="eyebrow">按交易日期</p>
                      <h3 id="trend-heading">每日经营趋势</h3>
                    </div>
                    <small>金额单位：元</small>
                  </header>
                  <TrendChart points={overview.trend} />
                </section>
                <aside className="dashboard-action-rail">
                  <div>
                    <p className="eyebrow">当前状态</p>
                    <strong>
                      {coverageStatus === "system_checked"
                        ? "经营结果已通过系统检查"
                        : coverageStatus === "review_required"
                          ? "金额可查看，仍需复核"
                          : "暂时没有结果"}
                    </strong>
                    <p>{overview.coverage.message}</p>
                  </div>
                  <dl>
                    <div>
                      <dt>待处理</dt>
                      <dd>{pendingCount.toLocaleString("zh-CN")} 项</dd>
                    </div>
                    <div>
                      <dt>等待处理店铺</dt>
                      <dd>
                        {Math.max(
                          0,
                          (catalog?.discoveredStoreCount ?? 0) -
                            (catalog?.processedStoreCount ?? 0)
                        ).toLocaleString("zh-CN")} 家
                      </dd>
                    </div>
                    <div>
                      <dt>利润</dt>
                      <dd>
                        {profit
                          ? `¥ ${formatMoney(profit)}`
                          : overview.coverage.profitStatus === "historical_pending"
                            ? "历史结果待复核"
                            : "暂不展示"}
                      </dd>
                    </div>
                  </dl>
                  <div className="rail-actions">
                    {multipleProcessedStores ? (
                      <button onClick={() => setDetail("stores")} type="button">
                        查看店铺对比
                      </button>
                    ) : null}
                    <button
                      className="secondary-button"
                      onClick={() => setDetail("transactions")}
                      type="button"
                    >
                      查看全部交易
                    </button>
                    <button
                      className="secondary-button"
                      onClick={() => setDetail("performance")}
                      type="button"
                    >
                      查看人员绩效
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setDetail("coverage")}
                      type="button"
                    >
                      查看数据覆盖
                    </button>
                  </div>
                </aside>
              </div>

              <section className="latest-preview" aria-labelledby="transactions-heading">
                <header>
                  <div>
                    <p className="eyebrow">最近发生</p>
                    <h3 id="transactions-heading">最新交易</h3>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => setDetail("transactions")}
                    type="button"
                  >
                    查看全部
                  </button>
                </header>
                <TransactionsView transactions={overview.transactions.slice(0, 3)} />
              </section>
            </>
          )}
        </>
      ) : null}

      <Modal
        onClose={() => setFilterOpen(false)}
        open={filterOpen}
        title="筛选经营范围"
      >
        <form
          className="modal-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (draftQuery.fromDate > draftQuery.toDate) return;
            setQuery(draftQuery);
            setFilterOpen(false);
          }}
        >
          <div className="field">
            <label htmlFor="analytics-platform">平台</label>
            <select
              disabled={platforms.length === 0}
              id="analytics-platform"
              onChange={(event) =>
                setDraftQuery((current) => ({
                  ...current,
                  platformId: event.target.value,
                  storeId: "all"
                }))
              }
              value={draftQuery.platformId}
            >
              <option value="all">
                {platforms.length ? "所有平台" : "平台尚未由服务分类"}
              </option>
              {platforms.map((platform) => (
                <option key={platform.id} value={platform.id}>
                  {platformLabel(platform)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="analytics-store">店铺</label>
            <select
              id="analytics-store"
              onChange={(event) => updateDraftQuery("storeId", event.target.value)}
              value={draftQuery.storeId}
            >
              <option value="all">所有已处理店铺</option>
              {stores.map((store) => (
                <option key={store.id} value={store.id}>
                  {storeLabel(store, platforms)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="analytics-period">月份</label>
            <select
              id="analytics-period"
              onChange={(event) => updateDraftQuery("period", event.target.value)}
              value={draftQuery.period}
            >
              <option value="all">全部可用月份</option>
              {overview?.filters.periods.map((period) => (
                <option key={period.value} value={period.value}>
                  {displayPeriod(period.value, period.label)}
                </option>
              ))}
            </select>
          </div>
          <div className="modal-form-row">
            <div className="field">
              <label htmlFor="analytics-from-date">起始日期</label>
              <input
                id="analytics-from-date"
                max={draftQuery.toDate || CURRENT_DATE}
                min={ANALYTICS_START_DATE}
                onChange={(event) => updateDraftQuery("fromDate", event.target.value)}
                type="date"
                value={draftQuery.fromDate}
              />
            </div>
            <div className="field">
              <label htmlFor="analytics-to-date">结束日期</label>
              <input
                id="analytics-to-date"
                max={CURRENT_DATE}
                min={draftQuery.fromDate || ANALYTICS_START_DATE}
                onChange={(event) => updateDraftQuery("toDate", event.target.value)}
                type="date"
                value={draftQuery.toDate}
              />
            </div>
          </div>
          {draftQuery.fromDate > draftQuery.toDate ? (
            <p className="inline-error" role="alert">起始日期不能晚于结束日期。</p>
          ) : null}
          <div className="modal-actions">
            <button
              className="secondary-button"
              onClick={() => setFilterOpen(false)}
              type="button"
            >
              取消
            </button>
            <button
              disabled={draftQuery.fromDate > draftQuery.toDate}
              type="submit"
            >
              应用范围
            </button>
          </div>
        </form>
      </Modal>
      <Modal
        onClose={() => setDetail(null)}
        open={detail === "stores"}
        size="wide"
        title="店铺经营对比"
      >
        {overview ? <StoreComparison rows={overview.storeBreakdown} /> : null}
      </Modal>
      <Modal
        onClose={() => setDetail(null)}
        open={detail === "transactions"}
        size="wide"
        title="当前范围全部交易"
      >
        {overview ? <TransactionsView transactions={overview.transactions} /> : null}
      </Modal>
      <Modal
        onClose={() => setDetail(null)}
        open={detail === "coverage"}
        title="当前数据覆盖"
      >
        {overview ? (
          <CoveragePanel
            catalog={catalog}
            catalogError={catalogError}
            periodCount={overview.filters.periods.length}
          />
        ) : null}
      </Modal>
      <Modal
        onClose={() => setDetail(null)}
        open={detail === "performance"}
        size="wide"
        title="人员、店铺与商品绩效"
      >
        <PerformancePanel
          period={performancePeriod(query.period)}
          store={query.storeId === "all" ? undefined : storeName}
        />
      </Modal>
    </section>
  );
}

const TASK_STATE_LABELS: Record<string, string> = {
  queued: "等待",
  waiting: "等待",
  running: "进行中",
  succeeded: "已完成",
  failed: "失败"
};

function ProgressView({
  data,
  scope
}: {
  data: DashboardData;
  scope: { storeId: string | null; period: string | null };
}) {
  const [detail, setDetail] = useState<"tasks" | "jobs" | "gates" | null>(null);
  const [progress, setProgress] = useState<ProgressData>(data.progress);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [computeDetailError, setComputeDetailError] = useState<string | null>(
    null
  );
  const [targetPlan, setTargetPlan] = useState<ComputeTargetPlan | null>(null);
  const [jobs, setJobs] = useState<ComputeJob[]>([]);
  const [runPhase, setRunPhase] = useState<"idle" | "running">("idle");
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState(() => new Date().toISOString());
  const walletMode = data.status.reconciliationMode === "platform_wallet";

  useEffect(() => {
    setProgress(data.progress);
  }, [data.progress]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let timer: number | undefined;
    const refresh = async () => {
      const [progressResult, targetResult, jobsResult] =
        await Promise.allSettled([
          loadProgress(scope, controller.signal),
          loadComputeTargets(controller.signal),
          loadComputeJobs(30, controller.signal)
        ]);
      if (!active) return;
      if (progressResult.status === "fulfilled") {
        setProgress(progressResult.value);
        setRefreshError(null);
        setRefreshedAt(new Date().toISOString());
      } else if (
        !(
          progressResult.reason instanceof DOMException &&
          progressResult.reason.name === "AbortError"
        )
      ) {
        setRefreshError(
          progressResult.reason instanceof Error
            ? progressResult.reason.message
            : "暂时无法更新处理进度"
        );
      }
      if (
        targetResult.status === "fulfilled" &&
        jobsResult.status === "fulfilled"
      ) {
        setTargetPlan(targetResult.value);
        setJobs(jobsResult.value);
        setComputeDetailError(null);
      } else {
        const reason =
          targetResult.status === "rejected"
            ? targetResult.reason
            : jobsResult.status === "rejected"
              ? jobsResult.reason
              : null;
        if (
          !(
            reason instanceof DOMException &&
            reason.name === "AbortError"
          )
        ) {
          setComputeDetailError(
            reason instanceof Error
              ? reason.message
              : "暂时无法读取全部店铺与月份"
          );
        }
      }
      if (active) timer = window.setTimeout(() => void refresh(), 5_000);
    };
    void refresh();
    return () => {
      active = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [scope.period, scope.storeId]);

  const startCompute = async () => {
    setRunPhase("running");
    setRunMessage(null);
    try {
      const result = await runCompute();
      setRunMessage(result.message);
      const [latestProgress, latestJobs, latestTargets] = await Promise.all([
        loadProgress(scope),
        loadComputeJobs(30),
        loadComputeTargets()
      ]);
      setProgress(latestProgress);
      setJobs(latestJobs);
      setTargetPlan(latestTargets);
      setComputeDetailError(null);
      setRefreshedAt(new Date().toISOString());
    } catch (cause) {
      setRunMessage(
        cause instanceof Error ? cause.message : "无法开始处理，请稍后重试"
      );
    } finally {
      setRunPhase("idle");
    }
  };

  const currentPeriod = progress.period
    ? displayPeriod(progress.period)
    : null;
  const taskSummary = progress.taskSummary;
  const taskStats = taskSummary
    ? [
        ["总任务", taskSummary.total],
        ["等待", taskSummary.waiting],
        ["进行中", taskSummary.running],
        ["成功", taskSummary.succeeded],
        ["失败", taskSummary.failed]
      ] as const
    : null;
  const targetStores = targetPlan
    ? new Set(targetPlan.targets.map((target) => target.logical_store_key)).size
    : 0;
  const targetPeriods = targetPlan
    ? Array.from(new Set(targetPlan.targets.map((target) => target.period))).sort()
    : [];
  const targetByStore = new Map(
    targetPlan?.targets.map((target) => [target.logical_store_key, target]) ?? []
  );
  const recentJobs = jobs.slice(0, 8);

  return (
    <section aria-labelledby="progress-heading" className="workspace-view">
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">实时更新</p>
          <h2 id="progress-heading">
            {progress.shop && progress.period
              ? `${progress.shop} · ${currentPeriod}`
              : "处理进度"}
          </h2>
          <p>每 5 秒从真实服务更新一次，不影响没有配置模型时的计算。</p>
        </div>
        <div className="summary-number">
          <span>未解释差额</span>
          <strong>¥ {formatMoney(progress.unexplainedAmount)}</strong>
        </div>
      </header>

      <div className="progress-refresh" aria-live="polite">
        <span>
          {refreshError
            ? "最近一次更新失败，继续显示上次结果"
            : `最近更新 ${formatDateTime(refreshedAt) || "刚刚"}`}
        </span>
        {refreshError ? <small>{refreshError}</small> : null}
      </div>

      {progress.compute ? (
        <section className="compute-control" aria-labelledby="compute-heading">
          <div>
            <p className="eyebrow">全部真实范围</p>
            <h3 id="compute-heading">检查所有店铺与月份</h3>
            <p>
              {targetPlan
                ? `${targetStores.toLocaleString("zh-CN")} 个店铺，${targetPlan.targets.length.toLocaleString("zh-CN")} 个店铺月份；${displayPeriodRange(targetPeriods)}。`
                : "正在读取本机已发现的店铺和月份。"}
            </p>
            {targetPlan?.review_required.length ? (
              <small>
                另有 {targetPlan.review_required.length.toLocaleString("zh-CN")}{" "}
                份文件需要先确认所属店铺。
              </small>
            ) : null}
          </div>
          <button
            disabled={!progress.compute.enabled || runPhase === "running"}
            onClick={() => void startCompute()}
            type="button"
          >
            {runPhase === "running"
              ? "正在开始…"
              : progress.compute.running
                ? "再次检查新文件"
                : "开始处理全部范围"}
          </button>
          {!progress.compute.enabled ? (
            <p className="compute-disabled">
              本机自动处理尚未启用；现有结果仍可查看，外部模型不影响确定性计算。
            </p>
          ) : null}
          {runMessage ? (
            <p className="compute-message" role="status">
              {runMessage}
            </p>
          ) : null}
          {computeDetailError ? (
            <p className="compute-detail-error">
              店铺月份计划或最近结果暂时不可读：{computeDetailError}
            </p>
          ) : null}
        </section>
      ) : null}

      {taskStats ? (
        <dl className="task-summary" aria-label="任务处理进度">
          {taskStats.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value.toLocaleString("zh-CN")}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="service-boundary" role="status">
          当前处理服务尚未返回总任务、等待、运行、成功和失败数量；这里不会用文件数或核对步骤冒充任务统计。
        </div>
      )}

      <section className="workspace-preview progress-overview">
        <header>
          <div>
            <p className="eyebrow">当前检查</p>
            <h3>质量门禁</h3>
          </div>
          <button
            className="text-button"
            onClick={() => setDetail("gates")}
            type="button"
          >
            查看全部
          </button>
        </header>
        <ul className="gate-glance">
          {progress.gates.slice(0, 4).map((gate) => (
            <li className={`gate-glance--${gate.state}`} key={gate.id}>
              <span>{gate.label}</span>
              <strong>{GATE_LABELS[gate.state]}</strong>
            </li>
          ))}
        </ul>
        <div className="workspace-actions">
          <button
            className="secondary-button"
            disabled={!progress.currentTasks?.length}
            onClick={() => setDetail("tasks")}
            type="button"
          >
            正在处理的范围
          </button>
          <button
            className="secondary-button"
            disabled={!recentJobs.length}
            onClick={() => setDetail("jobs")}
            type="button"
          >
            最近处理结果
          </button>
        </div>
      </section>

      <Modal
        onClose={() => setDetail(null)}
        open={detail === "tasks"}
        size="wide"
        title="正在处理的范围"
      >
      {progress.currentTasks?.length ? (
        <section className="current-tasks" aria-labelledby="current-tasks-heading">
          <header>
            <h3 id="current-tasks-heading">正在处理的范围</h3>
            <small>来自实时任务状态</small>
          </header>
          <ul>
            {progress.currentTasks.map((task, index) => (
              <li key={task.id ?? `${task.shop}-${task.period}-${index}`}>
                <div>
                  <strong>
                    {task.label ||
                      task.shop ||
                      (task.storeId
                        ? targetByStore.get(task.storeId)?.logical_store
                        : null) ||
                      "正在准备处理范围"}
                  </strong>
                  <span>
                    {[
                      task.platform ||
                        (task.storeId
                          ? targetByStore.get(task.storeId)?.platform
                          : null),
                      task.period ? displayPeriod(task.period) : null,
                      task.detail
                    ]
                      .filter(Boolean)
                      .join(" · ") || "正在读取店铺与月份"}
                  </span>
                </div>
                <em>
                  {TASK_STATE_LABELS[task.state ?? ""] ?? "处理中"}
                  {typeof task.progressPercent === "number"
                    ? ` ${task.progressPercent}%`
                    : ""}
                </em>
              </li>
            ))}
          </ul>
        </section>
      ) : <EmptyState>当前没有正在处理的范围。</EmptyState>}
      </Modal>

      <Modal
        onClose={() => setDetail(null)}
        open={detail === "jobs"}
        size="wide"
        title="最近处理结果"
      >
      {recentJobs.length ? (
        <section className="recent-jobs" aria-labelledby="recent-jobs-heading">
          <header>
            <h3 id="recent-jobs-heading">最近处理结果</h3>
            <small>只展示真实服务最近返回的 8 项</small>
          </header>
          <ul>
            {recentJobs.map((job) => (
              <li key={job.jobId}>
                <div>
                  <strong>{job.label}</strong>
                  <span>
                    {[
                      job.storeId
                        ? targetByStore.get(job.storeId)?.platform
                        : null,
                      job.period ? displayPeriod(job.period) : null,
                      job.detail,
                      formatDateTime(job.finishedAt ?? job.startedAt ?? job.createdAt)
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                  {job.error ? <small>{job.error}</small> : null}
                </div>
                <em className={`job-state job-state--${job.status}`}>
                  {TASK_STATE_LABELS[job.status] ?? job.status}
                </em>
              </li>
            ))}
          </ul>
        </section>
      ) : <EmptyState>当前还没有处理结果。</EmptyState>}
      </Modal>

      <Modal
        onClose={() => setDetail(null)}
        open={detail === "gates"}
        size="wide"
        title="全部质量检查"
      >
        <dl className="scope-strip" aria-label="当前核对范围">
          <div>
            <dt>核对范围</dt>
            <dd>
              {walletMode
                ? "订单 + 支付宝/微信平台钱包"
                : "订单 + 平台结算 + 银行资金"}
            </dd>
          </div>
          <div>
            <dt>银行流水</dt>
            <dd>
              {data.status.bankCashStatus === "not_applicable"
                ? "当前不纳入核对"
                : "本账期必须提供"}
            </dd>
          </div>
        </dl>
        <ol className="gate-list">
          {progress.gates.map((gate, index) => (
            <li className={`gate gate--${gate.state}`} key={gate.id}>
              <span className="gate-index">{String(index + 1).padStart(2, "0")}</span>
              <div>
                <div className="gate-title">
                  <h3>{gate.label}</h3>
                  <span>{GATE_LABELS[gate.state]}</span>
                </div>
                <p>{gate.detail}</p>
              </div>
            </li>
          ))}
        </ol>
      </Modal>
    </section>
  );
}

function BalancesTable({ rows }: { rows: DashboardData["balances"] }) {
  return (
    <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>核对环节</th>
              <th>业务编号</th>
              <th>应核金额</th>
              <th>对应金额</th>
              <th>已核金额</th>
              <th>未核差额</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const key = describeBalanceKey(row.balanceKey);
              return (
                <tr key={row.balanceId}>
                  <td>{key.scope}</td>
                  <td className="business-id">{key.identifier}</td>
                  <td className="numeric">{formatMoney(row.expectedAmount)}</td>
                  <td className="numeric">{formatMoney(row.actualAmount)}</td>
                  <td className="numeric">{formatMoney(row.matchedAmount)}</td>
                  <td className="numeric">{formatMoney(row.differenceAmount)}</td>
                  <td>
                    <span className={`status status--${row.status}`}>
                      {BALANCE_STATUS_LABELS[row.status]}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
    </div>
  );
}

function BalancesView({ data }: { data: DashboardData }) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  if (data.balances.length === 0) {
    return <EmptyState>还没有核对结果。完成文件冻结和核对运行后才会显示。</EmptyState>;
  }
  const totalDifference = data.balances.reduce(
    (sum, row) => sum + Number(row.differenceAmount),
    0
  );
  return (
    <section aria-labelledby="balances-heading" className="workspace-view">
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">系统核对结果</p>
          <h2 id="balances-heading">本月未核清明细</h2>
          <p>按未核差额从大到小展示；处理动作统一进入“待处理”。</p>
        </div>
        <button onClick={() => setDetailsOpen(true)} type="button">
          查看全部明细
        </button>
      </header>
      <dl className="compact-summary">
        <div>
          <dt>待核清记录</dt>
          <dd>{data.balances.length.toLocaleString("zh-CN")}</dd>
        </div>
        <div>
          <dt>当前未核差额</dt>
          <dd>¥ {formatMoney(String(totalDifference))}</dd>
        </div>
      </dl>
      <div className="workspace-preview">
        <header>
          <h3>差额最大的记录</h3>
          <span>显示前 8 项</span>
        </header>
        <BalancesTable rows={data.balances.slice(0, 8)} />
      </div>
      <Modal
        onClose={() => setDetailsOpen(false)}
        open={detailsOpen}
        size="wide"
        title="全部未核清明细"
      >
        <p className="result-note">
          当前服务返回 {data.balances.length.toLocaleString("zh-CN")} 项，完整确认清单可在“待处理”导出。
        </p>
        <BalancesTable rows={data.balances} />
      </Modal>
    </section>
  );
}

function ReviewRow({
  item,
  modelEnabled,
  onOpenEvidence,
  onSaved
}: {
  item: ReviewItem;
  modelEnabled: boolean;
  onOpenEvidence: (unresolvedId: string) => void;
  onSaved: () => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestion, setSuggestion] = useState<ReviewSuggestion | null>(null);
  const [suggestionError, setSuggestionError] = useState<string | null>(null);
  const save = async () => {
    if (!reason.trim()) return;
    setSaving(true);
    try {
      await decideReview(item.unresolvedId, "explain", reason.trim());
      await onSaved();
    } finally {
      setSaving(false);
    }
  };
  const suggest = async () => {
    setSuggesting(true);
    setSuggestionError(null);
    setSuggestion(null);
    try {
      const result = await suggestReviewExplanation(item.unresolvedId);
      setReason(result.suggestion);
      setSuggestion(result);
    } catch (cause) {
      setSuggestionError(
        cause instanceof Error ? cause.message : "模型未能生成业务说明"
      );
    } finally {
      setSuggesting(false);
    }
  };
  return (
    <li className="review-item review-item--issue">
      <details className="review-disclosure">
        <summary>
          <span className="review-summary-copy">
            <strong>{item.businessTitle}</strong>
            <small>
              {item.storeName} · {displayPeriod(item.period)} ·{" "}
              {item.businessSummary}
            </small>
          </span>
          <span className="review-summary-meta">
            <strong>¥ {formatMoney(item.amount)}</strong>
            <small>查看处理建议</small>
          </span>
        </summary>
        <div className="review-detail">
          <div className="review-guidance">
            <h4>建议先检查</h4>
            <p>{item.suggestedAction}</p>
            <button
              className="secondary-button evidence-button"
              disabled={item.evidenceCount === 0}
              onClick={() => onOpenEvidence(item.unresolvedId)}
              type="button"
            >
              {item.evidenceCount
                ? `打开原文件定位（${item.evidenceCount.toLocaleString("zh-CN")} 处）`
                : "暂无可打开的原始记录"}
            </button>
            {modelEnabled ? (
              <button
                className="secondary-button model-suggestion-button"
                disabled={suggesting}
                onClick={() => void suggest()}
                type="button"
              >
                {suggesting ? "正在生成说明…" : "用模型生成说明草案"}
              </button>
            ) : (
              <p className="model-availability-note">
                模型未启用；上面的确定性检查仍可继续，不影响金额核对。
              </p>
            )}
            {suggestion ? (
              <p
                className={
                  suggestion.evidenceGuard === "passed" &&
                  suggestion.reviewerStatus === "passed"
                    ? "inline-success"
                    : "inline-error"
                }
                role="status"
              >
                {suggestion.evidenceGuard !== "passed"
                  ? "模型没有引用到足够的原始依据，这份草案只能作为检查线索。"
                  : suggestion.reviewerStatus === "passed"
                    ? `${suggestion.model} 的原始引用已核验，${suggestion.reviewerModel ?? "独立模型"}复核通过；仍需你人工确认。`
                    : suggestion.reviewerStatus === "failed"
                      ? "原始引用已核验，但独立复核认为证据不足；草案不能视为已确认。"
                      : "原始引用已核验，但尚未完成独立复核；草案不能视为已确认。"}
              </p>
            ) : null}
            {suggestionError ? (
              <p className="inline-error" role="alert">
                {suggestionError}
              </p>
            ) : null}
          </div>
          <div className="review-action">
            <label htmlFor={`reason-${item.unresolvedId}`}>
              确认后的业务说明
            </label>
            <textarea
              id={`reason-${item.unresolvedId}`}
              onChange={(event) => setReason(event.target.value)}
              placeholder="例如：该笔为上月退款，本月平台钱包到账；已核对订单与退款记录"
              rows={4}
              value={reason}
            />
            <button disabled={saving || !reason.trim()} onClick={save} type="button">
              {saving ? "保存中…" : "保存已确认说明"}
            </button>
          </div>
        </div>
      </details>
    </li>
  );
}

function BusinessDecisionRow({
  item
}: {
  item: BusinessDecision;
}) {
  return (
    <li className="review-item">
      <div>
        <p className="eyebrow">
          {item.status === "decided" ? "候选口径记录" : "等待系统补充证据"}
        </p>
        <h3>{item.question}</h3>
        <p>{item.businessImpact}</p>
      </div>
      <div className="decision-evidence">
        <strong>
          {item.status === "decided"
            ? "已记录候选口径；是否执行以正式规则为准"
            : "未自动采用任何候选"}
        </strong>
        <p>
          {item.answer ??
            "现有证据不足以安全决定，系统会保留原文件并停止入账；规则改进后自动重跑。"}
        </p>
        {item.decidedAt ? (
          <small>记录时间：{formatDateTime(item.decidedAt)}</small>
        ) : null}
      </div>
    </li>
  );
}

function InputRevisionGroupRow({
  group
}: {
  group: InputRevisionGroup;
}) {
  return (
    <li className="input-revision-group">
      <header>
        <p className="eyebrow">
          {group.period} · {group.label}
        </p>
        <h3>系统尚未找到足够证据选定版本</h3>
        <p>
          同月同类文件出现多个版本，但控制金额、内容来源或完整性证据不足。
          系统不会让你凭文件名猜，也不会把多个版本混在一起计算；候选会进入规则学习记录。
        </p>
      </header>
      <ul className="input-revision-options">
        {group.candidates.map((candidate) => (
            <li key={candidate.revisionId}>
              <div>
                <strong>{safeFileName(candidate.originalName)}</strong>
                <span>
                  {safeSourceLabel(candidate.sourceLabel)} ·{" "}
                  {candidate.rowCount.toLocaleString("zh-CN")} 行
                </span>
                <p>{safeCandidateReason(candidate.reason)}</p>
              </div>
              <span className="candidate-state">保留证据，暂不入账</span>
            </li>
        ))}
      </ul>
    </li>
  );
}

function ReviewsView({
  data,
  scope,
  onOpenEvidence,
  onRefresh
}: {
  data: DashboardData;
  scope: { storeId: string | null; period: string | null };
  onOpenEvidence: (unresolvedId: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const [detail, setDetail] = useState<
    "revisions" | "decisions" | "differences" | null
  >(null);
  const [visibleReviewCount, setVisibleReviewCount] = useState(25);
  const [reviewItems, setReviewItems] = useState(data.reviews);
  const [reviewTotal, setReviewTotal] = useState(data.reviewTotal);
  const [activeGroup, setActiveGroup] = useState<ReviewGroup | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const useProblemGroups =
    data.reviewGroupTotal > 0 && data.reviewTotal > 500;
  useEffect(() => {
    setReviewItems(data.reviews);
    setReviewTotal(data.reviewTotal);
    setVisibleReviewCount(25);
    setActiveGroup(null);
  }, [data.reviews, data.reviewTotal]);
  const visibleReviews = reviewItems.slice(0, visibleReviewCount);
  const showMore = async () => {
    if (visibleReviewCount < reviewItems.length) {
      setVisibleReviewCount((count) => Math.min(count + 25, reviewItems.length));
      return;
    }
    if (reviewItems.length >= reviewTotal || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await loadReviewPage({
        storeId: activeGroup?.storeId ?? scope.storeId,
        period: activeGroup?.period ?? scope.period,
        reasonCode: activeGroup?.reasonCode,
        limit: 100,
        offset: reviewItems.length
      });
      setReviewItems((current) => [...current, ...page.items]);
      setReviewTotal(page.total);
      setVisibleReviewCount((count) => count + Math.min(25, page.items.length));
    } finally {
      setLoadingMore(false);
    }
  };
  const openProblemGroup = async (group: ReviewGroup) => {
    setLoadingMore(true);
    try {
      const page = await loadReviewPage({
        storeId: group.storeId,
        period: group.period,
        reasonCode: group.reasonCode,
        limit: 100,
        offset: 0
      });
      setActiveGroup(group);
      setReviewItems(page.items);
      setReviewTotal(page.total);
      setVisibleReviewCount(Math.min(25, page.items.length));
      setDetail("differences");
    } finally {
      setLoadingMore(false);
    }
  };
  const closeDifferences = () => {
    setDetail(null);
    setActiveGroup(null);
    setReviewItems(data.reviews);
    setReviewTotal(data.reviewTotal);
    setVisibleReviewCount(25);
  };
  const pendingDecisions = data.businessDecisions.filter(
    (item) => item.status === "pending"
  ).length;
  return (
    <section aria-labelledby="reviews-heading" className="workspace-view">
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">系统处理与学习</p>
          <h2 id="reviews-heading">待处理事项</h2>
          <p>先处理会影响入账和经营结果的事项；技术证据留在详情中。</p>
        </div>
        <a className="secondary-button" href={reviewExportUrl(scope)}>
          导出确认清单
        </a>
      </header>
      <div className="review-category-grid">
        <button
          className="review-category"
          disabled={!data.inputRevisionGroups.length}
          onClick={() => setDetail("revisions")}
          type="button"
        >
          <span>文件版本待学习</span>
          <strong>{data.inputRevisionGroups.length.toLocaleString("zh-CN")}</strong>
          <small>有证据后由系统确定使用版本</small>
        </button>
        <button
          className="review-category"
          disabled={!data.businessDecisions.length}
          onClick={() => setDetail("decisions")}
          type="button"
        >
          <span>业务口径记录</span>
          <strong>{data.businessDecisions.length.toLocaleString("zh-CN")}</strong>
          <small>{pendingDecisions ? `${pendingDecisions} 项等待证据` : "均有明确记录"}</small>
        </button>
        <button
          className="review-category review-category--urgent"
          disabled={!data.reviewTotal}
          onClick={() => setDetail("differences")}
          type="button"
        >
          <span>需要核对的问题</span>
          <strong>
            {(useProblemGroups
              ? data.reviewGroupTotal
              : data.reviewTotal
            ).toLocaleString("zh-CN")}
          </strong>
          <small>
            {useProblemGroups
              ? `已归并 ${data.reviewTotal.toLocaleString("zh-CN")} 条原始记录`
              : scope.storeId && scope.period
                ? "当前店铺与月份，按金额排序"
                : "当前可见经营范围，按金额排序"}
          </small>
        </button>
      </div>
      {data.reviewTotal ? (
        <section className="workspace-preview">
          <header>
            <div>
              <p className="eyebrow">优先处理</p>
              <h3>{useProblemGroups ? "影响最大的 3 组问题" : "差额最大的 3 项"}</h3>
            </div>
            <button
              className="text-button"
              onClick={() => setDetail("differences")}
              type="button"
            >
              查看全部
            </button>
          </header>
          {useProblemGroups ? (
            <ul className="review-list review-list--preview">
              {data.reviewGroups.slice(0, 3).map((group) => (
                <li className="review-preview-row" key={group.groupId}>
                  <div>
                    <strong>{group.businessTitle}</strong>
                    <span>
                      {group.storeName} · {displayPeriod(group.period)} ·{" "}
                      {group.itemCount.toLocaleString("zh-CN")} 条记录
                    </span>
                  </div>
                  <button
                    aria-label={`查看${group.storeName}${displayPeriod(group.period)}的${group.businessTitle}`}
                    className="review-preview-open"
                    disabled={loadingMore}
                    onClick={() => void openProblemGroup(group)}
                    type="button"
                  >
                    <b>¥ {formatMoney(group.absoluteAmount)}</b>
                    <small>逐条查看原始位置</small>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <ul className="review-list review-list--preview">
              {reviewItems.slice(0, 3).map((item) => (
              <li className="review-preview-row" key={item.unresolvedId}>
                <div>
                  <strong>{item.businessTitle}</strong>
                  <span>
                    {item.storeName} · {item.period} · {item.businessSummary}
                  </span>
                </div>
                <button
                  aria-label={`打开${item.storeName}${item.period}的原始记录`}
                  className="review-preview-open"
                  disabled={!item.evidenceCount}
                  onClick={() => onOpenEvidence(item.unresolvedId)}
                  type="button"
                >
                  <b>¥ {formatMoney(item.amount)}</b>
                  <small>{item.evidenceCount ? "打开原始行" : "暂无原始行"}</small>
                </button>
              </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
      {data.reviewTotal === 0 &&
      data.businessDecisions.length === 0 &&
      data.inputRevisionGroups.length === 0 ? (
        <EmptyState>当前没有待确认事项。</EmptyState>
      ) : null}
      <Modal
        onClose={() => setDetail(null)}
        open={detail === "revisions"}
        size="wide"
        title="待学习的文件版本"
      >
        <p className="result-note input-revision-intro">
          系统只在证据可复核时自动选定版本；以下候选保持阻断，不要求你代替规则做技术判断。
        </p>
        <ul className="input-revision-list">
          {data.inputRevisionGroups.map((group) => (
            <InputRevisionGroupRow group={group} key={group.groupId} />
          ))}
        </ul>
      </Modal>
      <Modal
        onClose={() => setDetail(null)}
        open={detail === "decisions"}
        size="wide"
        title="系统采用的业务口径"
      >
        <ul className="review-list">
          {data.businessDecisions.map((item) => (
            <BusinessDecisionRow item={item} key={item.decisionId} />
          ))}
        </ul>
      </Modal>
      <Modal
        onClose={closeDifferences}
        open={detail === "differences"}
        size="wide"
        title={activeGroup ? activeGroup.businessTitle : "需要核对的问题"}
      >
        {useProblemGroups && !activeGroup ? (
          <>
            <p className="result-note">
              系统已按店铺、月份和业务原因归并原始记录。先选择一组，再逐条打开原文件位置。
            </p>
            <ul className="review-group-list">
              {data.reviewGroups.map((group) => (
                <li key={group.groupId}>
                  <button
                    disabled={loadingMore}
                    onClick={() => void openProblemGroup(group)}
                    type="button"
                  >
                    <span>
                      <strong>{group.businessTitle}</strong>
                      <small>
                        {group.storeName} · {displayPeriod(group.period)} ·{" "}
                        {group.itemCount.toLocaleString("zh-CN")} 条记录
                      </small>
                    </span>
                    <span className="review-group-impact">
                      <b>¥ {formatMoney(group.absoluteAmount)}</b>
                      <small>查看原始位置</small>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            {activeGroup ? (
              <div className="review-group-context">
                <button
                  className="text-button"
                  onClick={() => {
                    setActiveGroup(null);
                    setReviewItems(data.reviews);
                    setReviewTotal(data.reviewTotal);
                    setVisibleReviewCount(25);
                  }}
                  type="button"
                >
                  返回问题分组
                </button>
                <p>
                  {activeGroup.storeName} · {displayPeriod(activeGroup.period)} ·{" "}
                  共 {reviewTotal.toLocaleString("zh-CN")} 条原始记录
                </p>
              </div>
            ) : (
              <p className="result-note">
                按差额从大到小展示，共 {reviewTotal.toLocaleString("zh-CN")} 项；
                导出清单严格服从当前店铺和月份。
              </p>
            )}
            <ul className="review-list">
              {visibleReviews.map((item) => (
                <ReviewRow
                  item={item}
                  key={item.unresolvedId}
                  modelEnabled={data.status.llmEnabled}
                  onOpenEvidence={onOpenEvidence}
                  onSaved={onRefresh}
                />
              ))}
            </ul>
            {visibleReviewCount < reviewTotal ? (
              <button
                className="secondary-button load-more"
                disabled={loadingMore}
                onClick={() => void showMore()}
                type="button"
              >
                {loadingMore ? "读取中…" : "再显示 25 项"}
              </button>
            ) : null}
          </>
        )}
      </Modal>
    </section>
  );
}

type LlmProtocolChoice = LlmProtocol | "auto";

const PROTOCOL_LABELS: Record<LlmProtocolChoice, string> = {
  auto: "自动识别（推荐）",
  openai_compatible: "OpenAI 协议",
  anthropic: "Anthropic 协议"
};

const DEFAULT_PROVIDER_URLS: Record<LlmProtocolChoice, string> = {
  auto: "",
  openai_compatible: "https://api.openai.com",
  anthropic: "https://api.anthropic.com"
};

function llmTaskPurpose(value: string | null): string {
  const labels: Record<string, string> = {
    connection_test: "连接验证",
    review_explanation_suggestion: "业务说明草案",
    anomaly_explanation: "差异说明",
    structure_classification: "文件用途识别"
  };
  return value ? labels[value] ?? "辅助说明" : "尚无任务";
}

function llmTaskResult(value: LlmPublicConfig["lastTaskStatus"]): string {
  if (value === "ok") return "成功";
  if (value === "error") return "失败";
  if (value === "pending") return "进行中";
  if (value === "disabled") return "未启用";
  return "尚无记录";
}

function ModelsView({
  data,
  onDashboardRefresh
}: {
  data: DashboardData;
  onDashboardRefresh: () => Promise<void>;
}) {
  const [configOpen, setConfigOpen] = useState(false);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const [config, setConfig] = useState<LlmPublicConfig | null>(null);
  const [capabilities, setCapabilities] = useState<CapabilityStatus | null>(null);
  const [protocol, setProtocol] = useState<LlmProtocolChoice>("auto");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [reviewerModel, setReviewerModel] = useState("");
  const [phase, setPhase] = useState<
    "loading" | "idle" | "discovering" | "applying" | "testing" | "disabling"
  >("loading");
  const [lastConnectionCheck, setLastConnectionCheck] = useState<
    "ok" | "error" | "disabled" | null
  >(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      loadLlmConfig(controller.signal),
      loadCapabilities(controller.signal)
    ])
      .then(([loaded, loadedCapabilities]) => {
        setConfig(loaded);
        setCapabilities(loadedCapabilities);
        if (loaded.protocol) setProtocol(loaded.protocol);
        if (loaded.baseUrl) setBaseUrl(loaded.baseUrl);
        if (loaded.selectedModel) setSelectedModel(loaded.selectedModel);
        if (loaded.reviewerModel) setReviewerModel(loaded.reviewerModel);
        if (
          loaded.lastTaskStatus === "ok" ||
          loaded.lastTaskStatus === "error" ||
          loaded.lastTaskStatus === "disabled"
        ) {
          setLastConnectionCheck(loaded.lastTaskStatus);
        }
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(
          cause instanceof Error ? cause.message : "无法读取模型连接设置"
        );
      })
      .finally(() => setPhase("idle"));
    return () => controller.abort();
  }, []);

  const changeProtocol = (next: LlmProtocolChoice) => {
    setProtocol(next);
    setBaseUrl(DEFAULT_PROVIDER_URLS[next]);
    setModels([]);
    setSelectedModel("");
    setReviewerModel("");
    setLastConnectionCheck(null);
    setMessage(null);
    setError(null);
  };

  const discover = async () => {
    if (!baseUrl.trim()) return;
    setPhase("discovering");
    setError(null);
    setMessage(null);
    try {
      const result = await discoverLlmModels({
        protocol,
        baseUrl: baseUrl.trim(),
        ...(apiKey ? { apiKey } : {})
      });
      setProtocol(result.protocol);
      setBaseUrl(result.baseUrl);
      setModels(result.models);
      setSelectedModel((current) =>
        result.models.includes(current) ? current : (result.models[0] ?? "")
      );
      setReviewerModel((current) =>
        result.models.includes(current) ? current : ""
      );
      setMessage(`已识别 ${result.models.length} 个可用模型，请选择后应用。`);
    } catch (cause) {
      setModels([]);
      setSelectedModel("");
      setReviewerModel("");
      setError(cause instanceof Error ? cause.message : "模型识别失败");
    } finally {
      setPhase("idle");
    }
  };

  const apply = async () => {
    if (!selectedModel || protocol === "auto") return;
    setPhase("applying");
    setError(null);
    setMessage(null);
    try {
      const saved = await applyLlmConfig({
        protocol,
        baseUrl: baseUrl.trim(),
        ...(apiKey ? { apiKey } : {}),
        selectedModel,
        ...(reviewerModel ? { reviewerModel } : {})
      });
      setConfig(saved);
      setApiKey("");
      setPhase("testing");
      const check = await testLlmConnection();
      setLastConnectionCheck(check.status);
      if (check.status === "ok") {
        setMessage(
          `已应用 ${selectedModel}，并完成一次真实模型调用。现在可在“待处理”中生成业务说明草案。`
        );
      } else {
        setLastConnectionCheck("error");
        setError(
          `配置已保存，但实际模型调用未通过：${check.message}`
        );
      }
      setConfig(await loadLlmConfig());
      await onDashboardRefresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "模型连接未能应用");
    } finally {
      setPhase("idle");
    }
  };

  const testConnection = async () => {
    setPhase("testing");
    setError(null);
    setMessage(null);
    try {
      const check = await testLlmConnection();
      setLastConnectionCheck(check.status);
      if (check.status === "ok") {
        setMessage(
          `${check.model} 已实际响应。现在可在“待处理”中生成业务说明草案。`
        );
      } else {
        setError(check.message);
      }
      setConfig(await loadLlmConfig());
    } catch (cause) {
      setLastConnectionCheck("error");
      setError(cause instanceof Error ? cause.message : "模型连接验证失败");
    } finally {
      setPhase("idle");
    }
  };

  const disable = async () => {
    setPhase("disabling");
    setError(null);
    setMessage(null);
    try {
      const saved = await disableLlm();
      setConfig(saved);
      setApiKey("");
      setModels([]);
      setReviewerModel("");
      setLastConnectionCheck("disabled");
      setMessage("已停用外部模型；确定性核对继续正常运行。");
      await onDashboardRefresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法停用模型连接");
    } finally {
      setPhase("idle");
    }
  };

  const configured = config?.enabled && config.configured;
  const effectiveLastStatus = lastConnectionCheck ?? config?.lastTaskStatus ?? null;
  const busy = phase !== "idle" && phase !== "loading";
  return (
    <section
      aria-labelledby="models-heading"
      className="workspace-view models-workspace"
    >
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">可选能力</p>
          <h2 id="models-heading">模型辅助</h2>
          <p>模型只辅助解释和归类，不参与金额计算。</p>
        </div>
        <button onClick={() => setConfigOpen(true)} type="button">
          配置模型连接
        </button>
      </header>
      <div className="settings-panel">
        <dl className="connection-summary">
          <div>
            <dt>当前模式</dt>
            <dd>
              {phase === "loading"
                ? "正在读取"
                : configured
                  ? `已启用 · ${config.selectedModel}`
                  : "未启用模型"}
            </dd>
          </div>
          <div>
            <dt>模型权限</dt>
            <dd>只提供建议（{data.status.autonomyLevel}）</dd>
          </div>
          <div>
            <dt>数据外发</dt>
            <dd>强制脱敏</dd>
          </div>
          <div>
            <dt>实际调用</dt>
            <dd>
              {effectiveLastStatus === "ok"
                ? "最近调用成功"
                : effectiveLastStatus === "error"
                  ? "最近调用失败"
                  : effectiveLastStatus === "pending"
                    ? "等待实际调用验证"
                  : configured
                    ? "尚未验证"
                    : "当前不适用"}
            </dd>
          </div>
        </dl>
        <section
          aria-live="polite"
          className={`llm-live-status llm-live-status--${
            configured ? effectiveLastStatus ?? "pending" : "disabled"
          }`}
        >
          <div>
            <strong>
              {configured
                ? `${config.selectedModel ?? "当前模型"} 已生效`
                : "外部模型未启用"}
            </strong>
            <span>
              {config?.updatedAt
                ? `配置更新于 ${formatDateTime(config.updatedAt)}`
                : "确定性核对可独立运行"}
            </span>
          </div>
          <dl>
            <div>
              <dt>最近任务</dt>
              <dd>{llmTaskPurpose(config?.lastTaskPurpose ?? null)}</dd>
            </div>
            <div>
              <dt>结果</dt>
              <dd>{llmTaskResult(effectiveLastStatus)}</dd>
            </div>
            <div>
              <dt>时间</dt>
              <dd>
                {config?.lastTaskAt
                  ? formatDateTime(config.lastTaskAt)
                  : "尚无记录"}
              </dd>
            </div>
          </dl>
          {config?.lastTaskMessage ? <p>{config.lastTaskMessage}</p> : null}
        </section>
        <p>
          金额、关联、分摊和账期状态始终由确定性代码处理。模型不可直接修改账本，
          未配置或调用失败时不影响对账。密钥只保存在本机工作台，不会由接口返回到浏览器。
        </p>
        <p className="model-purpose-note">
          模型启用后不会自动改变经营数字；它只在“待处理”中生成待你确认的业务说明草案。
        </p>
        {configured ? (
          <div className="model-role-summary">
            <div>
              <span>先提出解释</span>
              <strong>{config?.selectedModel}</strong>
            </div>
            <div>
              <span>再独立挑错</span>
              <strong>
                {config?.reviewerModel &&
                config.reviewerModel !== config.selectedModel
                  ? config.reviewerModel
                  : "尚未配置独立复核"}
              </strong>
            </div>
          </div>
        ) : null}
      </div>
      <div className="model-capability-summary">
        <div>
          <strong>模型只做建议，正式数字仍由本机核对</strong>
          <span>
            查看当前可用能力、证据复核状态和自动升级需要满足的条件。
          </span>
        </div>
        <button
          className="secondary-button"
          onClick={() => setCapabilitiesOpen(true)}
          type="button"
        >
          查看能力与学习门禁
        </button>
      </div>
      <Modal
        onClose={() => setCapabilitiesOpen(false)}
        open={capabilitiesOpen}
        size="wide"
        title="模型能做什么"
      >
        <section className="capability-panel" aria-labelledby="capability-heading">
        <div className="capability-intro">
          <div>
            <p className="eyebrow">当前可用范围</p>
            <h3 id="capability-heading">模型现在能帮你做什么</h3>
          </div>
          <p>
            当前权限为{" "}
            {capabilities?.effectiveLevel ?? data.status.autonomyLevel}：
            {capabilities?.levelReason ??
              "只能提供建议，金额和账本仍由确定性代码控制。"}
          </p>
        </div>
        {capabilities ? (
          <>
            <ul className="capability-list">
              {capabilities.tasks.map((task) => (
                <li key={task.id}>
                  <div>
                    <strong>{task.name}</strong>
                    <span>
                      {task.usesModel ? "使用模型辅助" : "本机确定性能力"}
                    </span>
                  </div>
                  <span className={`capability-state capability-state--${task.state}`}>
                    {task.state === "active"
                      ? "可用"
                      : task.state === "model_disabled"
                        ? "启用模型后可用"
                        : task.state === "reference_validation"
                          ? "参考数据核验中"
                          : "只评估，不自动发布"}
                  </span>
                </li>
              ))}
            </ul>
            <div className="learning-summary">
              <div>
                <span>已生成建议</span>
                <strong>{capabilities.learning.suggestionCount}</strong>
              </div>
              <div>
                <span>已人工复核</span>
                <strong>{capabilities.learning.reviewedCount}</strong>
              </div>
              <div>
                <span>已记录修正</span>
                <strong>{capabilities.learning.correctionCount}</strong>
              </div>
              <div>
                <span>通过证据核验</span>
                <strong>{capabilities.learning.evidenceGuardedCount}</strong>
              </div>
            </div>
            <p className="promotion-note">
              <strong>自动升级：</strong>
              {capabilities.learning.promotionEligible
                ? "已满足当前治理门禁，可进入人工发布评审。"
                : capabilities.learning.promotionReason}
            </p>
            <div className="model-boundary-panel">
              <div>
                <strong>模型能做什么</strong>
                <p>
                  当前已接线的是差额说明草案与独立复核。资料归类、字段映射、
                  关联建议和规则草案仍是受控策略，尚未作为页面能力启用。
                </p>
              </div>
              <div>
                <strong>模型永远不能做什么</strong>
                <p>不能修改金额，不能直接写账，也不能用两个模型的一致意见冒充事实。</p>
              </div>
              <div>
                <strong>何时才值得采纳</strong>
                <p>
                  必须引用真实原始行、由不同模型复核，并通过跨周期回归；否则只是一条待确认建议。
                </p>
              </div>
            </div>
          </>
        ) : (
          <p className="muted-copy">正在读取能力状态…</p>
        )}
        </section>
      </Modal>
      <Modal
        onClose={() => setConfigOpen(false)}
        open={configOpen}
        title="配置模型连接"
      >
        <form
          className="llm-form"
          onSubmit={(event) => {
            event.preventDefault();
            void (models.length ? apply() : discover());
          }}
        >
          <fieldset disabled={busy || phase === "loading"}>
            <legend>连接一个可选模型服务</legend>
            <div className="field">
              <label htmlFor="llm-protocol">接口协议</label>
              <select
                id="llm-protocol"
                onChange={(event) =>
                  changeProtocol(event.target.value as LlmProtocolChoice)
                }
                value={protocol}
              >
                {(Object.keys(PROTOCOL_LABELS) as LlmProtocolChoice[]).map((item) => (
                  <option key={item} value={item}>
                    {PROTOCOL_LABELS[item]}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="llm-url">接口地址</label>
              <input
                autoComplete="url"
                id="llm-url"
                onChange={(event) => {
                  setBaseUrl(event.target.value);
                  setModels([]);
                  setSelectedModel("");
                  setReviewerModel("");
                }}
                placeholder={DEFAULT_PROVIDER_URLS[protocol]}
                type="url"
                value={baseUrl}
              />
              <small>支持官方接口和同协议的本地网关地址。</small>
            </div>
            <div className="field">
              <label htmlFor="llm-key">
                API Key {config?.keyConfigured ? "（留空则沿用已保存密钥）" : ""}
              </label>
              <input
                autoComplete="off"
                id="llm-key"
                onChange={(event) => {
                  setApiKey(event.target.value);
                  setModels([]);
                  setSelectedModel("");
                  setReviewerModel("");
                }}
                placeholder="仅发送到本机服务"
                type="password"
                value={apiKey}
              />
            </div>
            {models.length ? (
              <div className="field">
                <label htmlFor="llm-model">负责提出解释的模型</label>
                <select
                  id="llm-model"
                  onChange={(event) => setSelectedModel(event.target.value)}
                  value={selectedModel}
                >
                  {models.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
                <small>它只生成草案，不会改动金额或账本。</small>
              </div>
            ) : null}
            {models.length > 1 ? (
              <div className="field">
                <label htmlFor="llm-reviewer-model">负责独立挑错的模型（推荐）</label>
                <select
                  id="llm-reviewer-model"
                  onChange={(event) => setReviewerModel(event.target.value)}
                  value={reviewerModel}
                >
                  <option value="">暂不启用独立复核</option>
                  {models
                    .filter((model) => model !== selectedModel)
                    .map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                </select>
                <small>应与主模型不同；复核失败时建议仍不会自动生效。</small>
              </div>
            ) : null}
            {error ? (
              <p className="inline-error" role="alert">
                {error}
              </p>
            ) : null}
            {message ? (
              <p className="success-message llm-message" role="status">
                {message}
              </p>
            ) : null}
            <div className="form-actions">
              {models.length ? (
                <button disabled={!selectedModel || busy} type="submit">
                  {phase === "applying" ? "应用中…" : "应用并立即生效"}
                </button>
              ) : (
                <button
                  disabled={!baseUrl.trim() || (!apiKey && !config?.keyConfigured) || busy}
                  type="submit"
                >
                  {phase === "discovering" ? "正在识别…" : "检测可用模型"}
                </button>
              )}
              {configured ? (
                <button
                  className="secondary-button"
                  disabled={busy}
                  onClick={() => void testConnection()}
                  type="button"
                >
                  {phase === "testing" ? "正在验证模型…" : "验证实际模型调用"}
                </button>
              ) : null}
              {configured ? (
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() => void disable()}
                  type="button"
                >
                  {phase === "disabling" ? "停用中…" : "停用外部模型"}
                </button>
              ) : null}
            </div>
          </fieldset>
        </form>
      </Modal>
    </section>
  );
}

export default function App() {
  const initialSearch = new URLSearchParams(window.location.search);
  const [view, setView] = useState<PrimaryView>("trust");
  const [overlay, setOverlay] = useState<OverlayView | null>(null);
  const [scope, setScope] = useState<{
    storeId: string | null;
    period: string | null;
  }>({
    storeId: initialSearch.get("store"),
    period: initialSearch.get("period")
  });
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [evidenceReviewId, setEvidenceReviewId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("review")
  );

  const openEvidence = useCallback((reviewId: string) => {
    const url = new URL(window.location.href);
    url.searchParams.set("review", reviewId);
    window.history.replaceState(null, "", url);
    setEvidenceReviewId(reviewId);
  }, []);

  const closeEvidence = useCallback(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete("review");
    window.history.replaceState(null, "", url);
    setEvidenceReviewId(null);
  }, []);

  const updateScope = useCallback(
    (next: { storeId: string; period: string }) => {
      const url = new URL(window.location.href);
      url.searchParams.set("store", next.storeId);
      url.searchParams.set("period", next.period);
      window.history.replaceState(null, "", url);
      setScope(next);
    },
    []
  );

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const loaded = await loadDashboard(scope);
      setData(loaded);
      setRefreshVersion((version) => version + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取对账服务");
    } finally {
      setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    const controller = new AbortController();
    loadDashboard(scope, controller.signal)
      .then(setData)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法读取对账服务");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [scope]);

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <p className="brand-kicker">只读原始数据 · 本机确定性计算</p>
          <h1>经营数据核验台</h1>
        </div>
        <div className="topbar-actions">
          {data ? (
            <span className={`mode mode--${data.status.mode}`}>
              {data.status.mode === "real"
                ? "真实数据"
                : data.status.mode === "synthetic"
                  ? "合成验证"
                  : "尚无数据"}
            </span>
          ) : null}
          <button className="secondary-button" onClick={refresh} type="button">
            刷新
          </button>
        </div>
      </header>

      <nav aria-label="工作台入口" className="nav">
        {PRIMARY_VIEWS.map((item) => (
          <button
            aria-current={view === item ? "page" : undefined}
            className={view === item ? "active" : ""}
            key={item}
            onClick={() => {
              setOverlay(null);
              setView(item);
            }}
            type="button"
          >
            {VIEW_LABELS[item]}
            {item === "reviews" &&
            data &&
            data.progress.unresolvedCount +
              data.businessDecisions.filter(
                (decision) => decision.status === "pending"
              ).length +
              data.inputRevisionGroups.length >
              0 ? (
              <span>
                {formatCount(
                  data.progress.unresolvedCount +
                    data.businessDecisions.filter(
                      (decision) => decision.status === "pending"
                    ).length +
                    data.inputRevisionGroups.length
                )}
              </span>
            ) : null}
          </button>
        ))}
      </nav>

      <main id="main">
        {loading ? <div className="skeleton" aria-label="正在读取真实服务" /> : null}
        {error ? (
          <div className="error-state" role="alert">
            <h2>无法连接对账服务</h2>
            <p>{error}</p>
            <button onClick={refresh} type="button">
              重新连接
            </button>
          </div>
        ) : null}
        {!loading && !error && data ? (
          <>
            {view === "trust" ? (
              <TrustCenter
                onOpenAnalytics={() => setView("analytics")}
                onOpenEvidence={openEvidence}
                onOpenProgress={() => setOverlay("progress")}
                onOpenReviews={() => setView("reviews")}
                onOpenBalances={() => setOverlay("balances")}
                onOpenCatalog={() => setOverlay("catalog")}
                onScopeChange={(cell) =>
                  updateScope({
                    storeId: cell.storeId,
                    period: cell.period
                  })
                }
                initialPeriod={scope.period}
                initialStoreId={scope.storeId}
                refreshVersion={refreshVersion}
              />
            ) : null}
            {view === "analytics" ? (
              <AnalyticsView data={data} initialScope={scope} />
            ) : null}
            {view === "reviews" ? (
              <ReviewsView
                data={data}
                onOpenEvidence={openEvidence}
                onRefresh={refresh}
                scope={scope}
              />
            ) : null}
            {view === "models" ? (
              <ModelsView data={data} onDashboardRefresh={refresh} />
            ) : null}
          </>
        ) : null}
      </main>
      {data ? (
        <Modal
          onClose={() => setOverlay(null)}
          open={overlay !== null}
          size="wide"
          title={
            overlay === "catalog"
              ? "数据覆盖"
              : overlay === "progress"
                ? "实时处理进度"
                : "核对明细"
          }
        >
          {overlay === "catalog" ? <CatalogView /> : null}
          {overlay === "progress" ? (
            <ProgressView data={data} scope={scope} />
          ) : null}
          {overlay === "balances" ? <BalancesView data={data} /> : null}
        </Modal>
      ) : null}
      {evidenceReviewId ? (
        <EvidenceWorkbench
          onClose={closeEvidence}
          unresolvedId={evidenceReviewId}
        />
      ) : null}
    </div>
  );
}
