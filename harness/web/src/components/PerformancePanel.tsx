import { useEffect, useState } from "react";

import {
  loadPerformanceOverview,
  loadPerformancePeople
} from "../api";
import type {
  PerformanceOverview,
  PerformancePeople
} from "../types";

function money(value: string): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4
  }).format(number);
}

function displayPeriod(value: string | null): string {
  if (!value || !/^\d{4}$/.test(value)) return "尚无月份";
  return `20${value.slice(0, 2)} 年 ${Number(value.slice(2))} 月`;
}

const gateCopy: Record<string, string> = {
  product_grain_missing: "订单或经营结果还没有可按商品追溯的编码。",
  product_metric_missing: "部分商品缺少销售、退款、费用或成本中的一项。",
  cost_coverage_insufficient: "部分已售商品没有可核对的商品成本。",
  product_identity_missing: "源文件里的商品编码还不能唯一对应到商品主表。",
  assignment_missing: "部分商品没有找到在本月生效的负责人。",
  assignment_conflict: "同一商品在本月对应了互相冲突的负责人。",
  evidence_missing: "部分绩效金额不能定位到原始文件行。",
  locked_period_change: "该账期已锁定，修订需要走明确更正流程。"
};

export function PerformancePanel({
  period,
  store
}: {
  period?: string;
  store?: string;
}) {
  const [mode, setMode] = useState<"single" | "combined">("single");
  const [overview, setOverview] = useState<PerformanceOverview | null>(null);
  const [people, setPeople] = useState<PerformancePeople | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      loadPerformanceOverview(mode, period, store, controller.signal),
      loadPerformancePeople(mode, period, store, controller.signal)
    ])
      .then(([nextOverview, nextPeople]) => {
        setOverview(nextOverview);
        setPeople(nextPeople);
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法读取人员绩效对照");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [mode, period, store]);

  if (loading) {
    return <div aria-label="正在读取人员绩效" className="skeleton performance-skeleton" />;
  }
  if (error) {
    return <div className="inline-error" role="alert">{error}</div>;
  }
  if (!overview || !people || overview.rowCount === 0) {
    return (
      <div className="evidence-empty">
        已接入负责人主表，但当前范围还没有可核对的历史绩效文件。
      </div>
    );
  }
  const gate = overview.engineGate;
  const certifiedForScope =
    overview.certifiedPerformanceAvailable === true &&
    overview.referenceOnly === false &&
    gate?.status === "certified";
  const gateMessage =
    (gate?.code && gateCopy[gate.code]) ||
    gate?.message ||
    "当前范围尚未完成商品级认证绩效计算。";

  return (
    <section className="performance-panel">
      <header className="performance-toolbar">
        <div>
          <p className="eyebrow">人员 × 店铺 × 商品</p>
          <h3>{displayPeriod(overview.period)}绩效对照</h3>
          <p>
            这些数字来自原有单算/合算文件，用于复核新引擎，不会冒充认证绩效。
          </p>
        </div>
        <div aria-label="历史计算方式" className="segmented-control">
          <button
            aria-pressed={mode === "single"}
            className={mode === "single" ? "active" : ""}
            onClick={() => setMode("single")}
            type="button"
          >
            单算
          </button>
          <button
            aria-pressed={mode === "combined"}
            className={mode === "combined" ? "active" : ""}
            onClick={() => setMode("combined")}
            type="button"
          >
            合算
          </button>
        </div>
      </header>
      <div className="performance-trust-note">
        <strong>
          {certifiedForScope ? "当前范围已有认证绩效" : "当前只显示历史对照"}
        </strong>
        <span>{certifiedForScope ? "每个结果都绑定商品、负责人、账期和原始行。" : gateMessage}</span>
      </div>
      <dl className="performance-quality">
        <div data-state={overview.assignment.conflictCount ? "blocked" : "passed"}>
          <dt>负责人归属</dt>
          <dd>
            {overview.assignment.conflictCount
              ? `${overview.assignment.conflictCount} 项冲突，暂不发布`
              : `${overview.assignment.activeCount} 项有效归属`}
          </dd>
        </div>
        <div data-state={certifiedForScope ? "passed" : "pending"}>
          <dt>商品利润证据</dt>
          <dd>{certifiedForScope ? "已逐项核验" : "仍在补齐，未作为考核依据"}</dd>
        </div>
        <div data-state="reference">
          <dt>历史结果对照</dt>
          <dd>
            {overview.formulaPassCount.toLocaleString("zh-CN")} /{" "}
            {overview.rowCount.toLocaleString("zh-CN")} 行可复现；这不是准确率
          </dd>
        </div>
      </dl>
      <dl className="performance-metrics">
        <div><dt>人员</dt><dd>{overview.personCount}</dd></div>
        <div><dt>店铺</dt><dd>{overview.storeCount}</dd></div>
        <div><dt>商品</dt><dd>{overview.productCount.toLocaleString("zh-CN")}</dd></div>
        <div><dt>交易收款</dt><dd>¥ {money(overview.metrics.collectedAmount)}</dd></div>
        <div>
          <dt>{certifiedForScope ? "认证经营利润" : "历史店铺利润"}</dt>
          <dd>¥ {money(overview.metrics.storeProfit)}</dd>
        </div>
      </dl>
      <div className="performance-table-scroll">
        <table className="performance-table">
          <thead>
            <tr>
              <th>负责人</th>
              <th>店铺</th>
              <th>商品数</th>
              <th>交易收款</th>
              <th>退款</th>
              <th>商品成本</th>
              <th>广告费</th>
              <th>历史利润</th>
            </tr>
          </thead>
          <tbody>
            {people.rows.map((row) => (
              <tr key={`${row.personId}-${row.storeName}`}>
                <td>{row.personName}</td>
                <td>{row.storeName}</td>
                <td>{row.productCount.toLocaleString("zh-CN")}</td>
                <td>¥ {money(row.collectedAmount)}</td>
                <td>¥ {money(row.refundAmount)}</td>
                <td>¥ {money(row.productCost)}</td>
                <td>¥ {money(row.advertisingFee)}</td>
                <td>¥ {money(row.storeProfit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
