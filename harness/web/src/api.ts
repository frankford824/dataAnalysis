import type {
  AnalyticsCatalog,
  AnalyticsOverview,
  AnalyticsPeriod,
  AnalyticsQuery,
  CapabilityStatus,
  ComputeJob,
  ComputeProgress,
  ComputeRunResult,
  ComputeTargetPlan,
  DashboardData,
  LlmDiscoveryResult,
  LlmProtocol,
  LlmPublicConfig,
  LlmTestResult,
  PerformanceOverview,
  PerformancePeople,
  ProgressData,
  ProgressTask,
  ProgressTaskSummary,
  ReviewGroupPage,
  ReviewPage,
  ReviewEvidenceDetail,
  ReviewEvidencePreview,
  ReviewSuggestion,
  TrustMatrix
} from "./types";

const API_BASE = "/api/v1";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal
  });
  if (!response.ok) {
    const detail = await response.text();
    if (response.status >= 500) {
      throw new Error("服务正在更新数据，请稍后刷新重试");
    }
    if (detail) {
      let parsedDetail: string | undefined;
      try {
        const parsed = JSON.parse(detail) as { detail?: string };
        parsedDetail = parsed.detail;
      } catch {
        parsedDetail = undefined;
      }
      if (parsedDetail) throw new Error(parsedDetail);
    }
    throw new Error(detail || `服务返回 ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  const text = await response.text();
  if (response.status >= 500) {
    return new Error(fallback);
  }
  if (text) {
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      if (parsed.detail) return new Error(parsed.detail);
    } catch {
      return new Error(fallback);
    }
  }
  return new Error(fallback);
}

export async function loadReviewPage(
  input: {
    storeId: string | null;
    period: string | null;
    reasonCode?: string | null;
    limit?: number;
    offset?: number;
  },
  signal?: AbortSignal
): Promise<ReviewPage> {
  const query = new URLSearchParams({
    limit: String(input.limit ?? 100),
    offset: String(input.offset ?? 0)
  });
  if (input.storeId) query.set("storeId", input.storeId);
  if (input.period) query.set("period", input.period);
  if (input.reasonCode) query.set("reasonCode", input.reasonCode);
  return getJson<ReviewPage>(`/reviews/page?${query.toString()}`, signal);
}

export async function loadReviewGroups(
  input: {
    storeId: string | null;
    period: string | null;
    limit?: number;
  },
  signal?: AbortSignal
): Promise<ReviewGroupPage> {
  const query = new URLSearchParams({
    limit: String(input.limit ?? 100)
  });
  if (input.storeId) query.set("storeId", input.storeId);
  if (input.period) query.set("period", input.period);
  return getJson<ReviewGroupPage>(`/reviews/groups?${query.toString()}`, signal);
}

export async function loadDashboard(
  scope?: { storeId: string | null; period: string | null },
  signal?: AbortSignal
): Promise<DashboardData> {
  const scopeQuery = new URLSearchParams();
  if (scope?.storeId) scopeQuery.set("storeId", scope.storeId);
  if (scope?.period) scopeQuery.set("period", scope.period);
  const scopeSuffix = scopeQuery.toString();
  const scopedReviews = loadReviewPage(
    {
      storeId: scope?.storeId ?? null,
      period: scope?.period ?? null
    },
    signal
  );
  const scopedReviewGroups = loadReviewGroups(
    {
      storeId: scope?.storeId ?? null,
      period: scope?.period ?? null
    },
    signal
  );
  const [
    status,
    progress,
    balances,
    reviewPage,
    reviewGroupPage,
    businessDecisions,
    inputRevisionGroups
  ] = await Promise.all([
    getJson<DashboardData["status"]>("/status", signal),
    loadProgress(scope, signal),
    getJson<DashboardData["balances"]>(
      `/balances${scopeSuffix ? `?${scopeSuffix}` : ""}`,
      signal
    ),
    scopedReviews,
    scopedReviewGroups,
    getJson<DashboardData["businessDecisions"]>("/business-decisions", signal),
    getJson<DashboardData["inputRevisionGroups"]>("/input-revisions", signal)
  ]);
  return {
    status,
    progress,
    balances,
    reviews: reviewPage.items,
    reviewTotal: reviewPage.total,
    reviewGroups: Array.isArray(reviewGroupPage?.groups)
      ? reviewGroupPage.groups
      : [],
    reviewGroupTotal:
      typeof reviewGroupPage?.groupCount === "number"
        ? reviewGroupPage.groupCount
        : 0,
    businessDecisions,
    inputRevisionGroups
  };
}

type ComputeCurrentWire = {
  jobId: string;
  label: string;
  detail?: string | null;
  status: string;
  progressPercent: number;
  storeId?: string | null;
  period?: string | null;
};

type ComputeProgressWire = Partial<ComputeProgress> & {
  current?: ComputeCurrentWire[];
};

type ProgressWire = Omit<
  ProgressData,
  "taskSummary" | "currentTasks" | "compute"
> & {
  taskSummary?: Partial<ProgressTaskSummary>;
  tasks?: ProgressTask[];
  currentTasks?: ProgressTask[];
  compute?: ComputeProgressWire;
  totalTasks?: number;
  waitingTasks?: number;
  queuedTasks?: number;
  pendingTasks?: number;
  runningTasks?: number;
  succeededTasks?: number;
  completedTasks?: number;
  failedTasks?: number;
};

function taskCount(...values: Array<number | undefined>): number | undefined {
  return values.find(
    (value): value is number =>
      typeof value === "number" && Number.isFinite(value) && value >= 0
  );
}

function normalizeProgress(payload: ProgressWire): ProgressData {
  const nested = payload.taskSummary;
  const compute = payload.compute;
  const total = taskCount(nested?.total, compute?.total, payload.totalTasks);
  const waiting = taskCount(
    nested?.waiting,
    compute?.queued,
    payload.waitingTasks,
    payload.queuedTasks,
    payload.pendingTasks
  );
  const running = taskCount(
    nested?.running,
    compute?.active,
    payload.runningTasks
  );
  const succeeded = taskCount(
    nested?.succeeded,
    compute?.succeeded,
    payload.succeededTasks,
    payload.completedTasks
  );
  const failed = taskCount(nested?.failed, compute?.failed, payload.failedTasks);
  const hasCompleteSummary =
    total !== undefined &&
    waiting !== undefined &&
    running !== undefined &&
    succeeded !== undefined &&
    failed !== undefined;
  const currentTasks =
    compute?.current?.map((task) => ({
      id: task.jobId,
      label: task.label,
      storeId: task.storeId,
      period: task.period,
      state: task.status === "queued" ? "waiting" : task.status,
      progressPercent: task.progressPercent,
      detail: task.detail
    })) ??
    payload.currentTasks ??
    payload.tasks?.filter(
      (task) => task.state === "running" || task.state === "waiting"
    );
  const normalizedCompute =
    compute &&
    typeof compute.enabled === "boolean" &&
    typeof compute.running === "boolean"
      ? {
          enabled: compute.enabled,
          running: compute.running,
          cycleId: compute.cycleId ?? null,
          total: total ?? 0,
          queued: waiting ?? 0,
          active: running ?? 0,
          succeeded: succeeded ?? 0,
          failed: failed ?? 0
        }
      : undefined;
  const {
    taskSummary: _taskSummary,
    tasks: _tasks,
    currentTasks: _currentTasks,
    compute: _compute,
    ...base
  } = payload;
  return {
    ...base,
    ...(hasCompleteSummary
      ? {
          taskSummary: {
            total,
            waiting,
            running,
            succeeded,
            failed
          }
        }
      : {}),
    ...(currentTasks ? { currentTasks } : {}),
    ...(normalizedCompute ? { compute: normalizedCompute } : {})
  };
}

export async function loadProgress(
  scope?: { storeId: string | null; period: string | null },
  signal?: AbortSignal
): Promise<ProgressData> {
  const query = new URLSearchParams();
  if (scope?.storeId) query.set("storeId", scope.storeId);
  if (scope?.period) query.set("period", scope.period);
  const suffix = query.toString();
  const payload = await getJson<ProgressWire>(
    `/progress${suffix ? `?${suffix}` : ""}`,
    signal
  );
  return normalizeProgress(payload);
}

export async function loadComputeJobs(
  limit = 30,
  signal?: AbortSignal
): Promise<ComputeJob[]> {
  const payload = await getJson<unknown>(
    `/compute/jobs?limit=${encodeURIComponent(String(limit))}`,
    signal
  );
  if (!Array.isArray(payload)) {
    throw new Error("最近处理结果格式无效");
  }
  return payload as ComputeJob[];
}

export async function loadComputeTargets(
  signal?: AbortSignal
): Promise<ComputeTargetPlan> {
  const payload = await getJson<Partial<ComputeTargetPlan>>(
    "/compute/targets",
    signal
  );
  if (
    !payload ||
    !Array.isArray(payload.targets) ||
    !Array.isArray(payload.review_required) ||
    typeof payload.scope_start !== "string" ||
    typeof payload.scope_end !== "string"
  ) {
    throw new Error("店铺月份计划格式无效");
  }
  return payload as ComputeTargetPlan;
}

export async function runCompute(): Promise<ComputeRunResult> {
  const response = await fetch(`${API_BASE}/compute/run`, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json" }
  });
  if (!response.ok) {
    throw await responseError(response, "无法开始处理");
  }
  return response.json() as Promise<ComputeRunResult>;
}

type AnalyticsOverviewWire = Omit<
  AnalyticsOverview,
  "filters" | "selection" | "metrics"
> & {
  filters: {
    platforms?: AnalyticsOverview["filters"]["platforms"];
    stores: AnalyticsOverview["filters"]["stores"];
    periods: Array<
      AnalyticsPeriod & {
        id?: string;
        fromDate?: string;
        toDate?: string;
      }
    >;
    dateRange: {
      min?: string | null;
      max?: string | null;
      fromDate?: string | null;
      toDate?: string | null;
    };
    selected?: AnalyticsOverview["selection"];
  };
  selection?: AnalyticsOverview["selection"];
  metrics?: AnalyticsOverview["metrics"];
  summary?: AnalyticsOverview["metrics"];
};

function normalizeAnalyticsOverview(
  payload: AnalyticsOverviewWire
): AnalyticsOverview {
  const metrics = payload.metrics ?? payload.summary;
  if (!metrics) {
    throw new Error("经营数据缺少汇总结果");
  }
  const selection = payload.selection ?? payload.filters.selected;
  if (!selection) {
    throw new Error("经营数据缺少当前筛选范围");
  }
  return {
    ...payload,
    filters: {
      platforms: payload.filters.platforms,
      stores: payload.filters.stores,
      periods: payload.filters.periods.map((item) => ({
        value: item.value ?? item.id ?? "",
        label: item.label
      })),
      dateRange: {
        min:
          payload.filters.dateRange.min ??
          payload.filters.dateRange.fromDate ??
          null,
        max:
          payload.filters.dateRange.max ??
          payload.filters.dateRange.toDate ??
          null
      }
    },
    selection,
    metrics
  };
}

export async function loadAnalyticsOverview(
  query: AnalyticsQuery,
  signal?: AbortSignal
): Promise<AnalyticsOverview> {
  const parameters = new URLSearchParams({
    storeId: query.storeId,
    period: query.period,
    limit: String(query.limit ?? 50)
  });
  if (query.platformId && query.platformId !== "all") {
    parameters.set("platformId", query.platformId);
  }
  if (query.fromDate) parameters.set("fromDate", query.fromDate);
  if (query.toDate) parameters.set("toDate", query.toDate);
  const payload = await getJson<AnalyticsOverviewWire>(
    `/analytics/overview?${parameters.toString()}`,
    signal
  );
  return normalizeAnalyticsOverview(payload);
}

export async function loadAnalyticsCatalog(
  signal?: AbortSignal
): Promise<AnalyticsCatalog> {
  return getJson<AnalyticsCatalog>("/analytics/catalog", signal);
}

export async function loadPerformanceOverview(
  calculationMode: "single" | "combined",
  period?: string,
  store?: string,
  signal?: AbortSignal
): Promise<PerformanceOverview> {
  const parameters = new URLSearchParams({ calculationMode });
  if (period) parameters.set("period", period);
  if (store) parameters.set("store", store);
  return getJson<PerformanceOverview>(
    `/performance/overview?${parameters.toString()}`,
    signal
  );
}

export async function loadPerformancePeople(
  calculationMode: "single" | "combined",
  period?: string,
  store?: string,
  signal?: AbortSignal
): Promise<PerformancePeople> {
  const parameters = new URLSearchParams({ calculationMode, limit: "100" });
  if (period) parameters.set("period", period);
  if (store) parameters.set("store", store);
  return getJson<PerformancePeople>(
    `/performance/people?${parameters.toString()}`,
    signal
  );
}

export async function decideReview(
  unresolvedId: string,
  decision: "explain" | "defer" | "reject",
  reason: string
): Promise<void> {
  const response = await fetch(`${API_BASE}/reviews/${encodeURIComponent(unresolvedId)}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, reason })
  });
  if (!response.ok) {
    throw await responseError(response, "保存说明失败");
  }
}

export async function suggestReviewExplanation(
  unresolvedId: string
): Promise<ReviewSuggestion> {
  const response = await fetch(
    `${API_BASE}/reviews/${encodeURIComponent(unresolvedId)}/suggestion`,
    {
      method: "POST",
      credentials: "same-origin"
    }
  );
  if (!response.ok) {
    throw await responseError(response, "模型未能生成业务说明");
  }
  return response.json() as Promise<ReviewSuggestion>;
}

export async function loadReviewEvidence(
  unresolvedId: string,
  signal?: AbortSignal
): Promise<ReviewEvidenceDetail> {
  return getJson<ReviewEvidenceDetail>(
    `/reviews/${encodeURIComponent(unresolvedId)}/evidence`,
    signal
  );
}

export async function loadReviewEvidencePreview(
  unresolvedId: string,
  snapshotId: string,
  signal?: AbortSignal
): Promise<ReviewEvidencePreview> {
  return getJson<ReviewEvidencePreview>(
    `/reviews/${encodeURIComponent(unresolvedId)}/evidence/${encodeURIComponent(snapshotId)}/preview`,
    signal
  );
}

export function reviewEvidenceOriginalUrl(
  unresolvedId: string,
  snapshotId: string
): string {
  return (
    `${API_BASE}/reviews/${encodeURIComponent(unresolvedId)}/evidence/` +
    `${encodeURIComponent(snapshotId)}/original`
  );
}

export async function loadTrustMatrix(
  signal?: AbortSignal
): Promise<TrustMatrix> {
  return getJson<TrustMatrix>("/trust/matrix", signal);
}

export function reviewExportUrl(scope?: {
  storeId: string | null;
  period: string | null;
}): string {
  const query = new URLSearchParams();
  if (scope?.storeId) query.set("storeId", scope.storeId);
  if (scope?.period) query.set("period", scope.period);
  const suffix = query.toString();
  return `${API_BASE}/reviews.csv${suffix ? `?${suffix}` : ""}`;
}

export async function decideBusinessQuestion(
  decisionId: string,
  answer: string
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/business-decisions/${encodeURIComponent(decisionId)}`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer })
    }
  );
  if (!response.ok) {
    throw new Error((await response.text()) || "保存业务口径失败");
  }
}

export async function selectInputRevision(
  revisionId: string,
  reason: string
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/input-revisions/${encodeURIComponent(revisionId)}/select`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason })
    }
  );
  if (!response.ok) {
    throw new Error((await response.text()) || "保存原始文件选择失败");
  }
}

export async function loadLlmConfig(signal?: AbortSignal): Promise<LlmPublicConfig> {
  return getJson<LlmPublicConfig>("/llm/config", signal);
}

export async function loadCapabilities(
  signal?: AbortSignal
): Promise<CapabilityStatus> {
  return getJson<CapabilityStatus>("/capabilities", signal);
}

export async function discoverLlmModels(input: {
  protocol: LlmProtocol | "auto";
  baseUrl: string;
  apiKey?: string;
}): Promise<LlmDiscoveryResult> {
  const response = await fetch(`${API_BASE}/llm/discover`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input)
  });
  if (!response.ok) {
    throw await responseError(response, "无法读取此接口支持的模型");
  }
  return response.json() as Promise<LlmDiscoveryResult>;
}

export async function applyLlmConfig(input: {
  protocol: LlmProtocol;
  baseUrl: string;
  apiKey?: string;
  selectedModel: string;
  reviewerModel?: string;
}): Promise<LlmPublicConfig> {
  const response = await fetch(`${API_BASE}/llm/config`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...input, enabled: true })
  });
  if (!response.ok) {
    throw await responseError(response, "模型连接未能应用");
  }
  return response.json() as Promise<LlmPublicConfig>;
}

export async function testLlmConnection(): Promise<LlmTestResult> {
  const response = await fetch(`${API_BASE}/llm/test`, {
    method: "POST",
    credentials: "same-origin"
  });
  if (!response.ok) {
    throw await responseError(response, "模型连接验证失败");
  }
  return response.json() as Promise<LlmTestResult>;
}

export async function disableLlm(): Promise<LlmPublicConfig> {
  const response = await fetch(`${API_BASE}/llm/config`, {
    method: "DELETE",
    credentials: "same-origin"
  });
  if (!response.ok) {
    throw await responseError(response, "无法停用模型连接");
  }
  return response.json() as Promise<LlmPublicConfig>;
}
