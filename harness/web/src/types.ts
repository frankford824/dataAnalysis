export type GateState = "complete" | "active" | "blocked" | "pending";

export interface Gate {
  id: string;
  label: string;
  state: GateState;
  detail: string;
}

export interface WorkbenchStatus {
  mode: "real" | "synthetic" | "empty";
  workspace: string;
  schemaVersion: number | null;
  llmEnabled: boolean;
  llmConfigured: boolean;
  autonomyLevel: "L0" | "L1" | "L2";
  reconciliationMode: "platform_wallet" | "bank_three_way";
  bankCashStatus: "not_applicable" | "required";
  readOnlySourceEnforced: boolean;
  updatedAt: string;
}

export type LlmProtocol = "openai_compatible" | "anthropic";

export interface LlmPublicConfig {
  enabled: boolean;
  configured: boolean;
  protocol: LlmProtocol | null;
  baseUrl: string | null;
  selectedModel: string | null;
  reviewerModel: string | null;
  keyConfigured: boolean;
  completionSupported: boolean;
  detail: string;
  updatedAt: string | null;
  lastTaskStatus: "pending" | "ok" | "error" | "disabled" | null;
  lastTaskPurpose: string | null;
  lastTaskModel: string | null;
  lastTaskMessage: string | null;
  lastTaskAt: string | null;
}

export interface LlmDiscoveryResult {
  protocol: LlmProtocol;
  baseUrl: string;
  models: string[];
  completionSupported: boolean;
}

export interface LlmTestResult {
  status: "ok" | "error" | "disabled";
  model: string;
  message: string;
  requestId: string | null;
}

export interface CapabilityTask {
  id: string;
  name: string;
  state:
    | "active"
    | "model_disabled"
    | "reference_validation"
    | "evaluation_only";
  usesModel: boolean;
  mayWriteLedger: false;
}

export interface CapabilityStatus {
  effectiveLevel: "L0" | "L1" | "L2";
  levelReason: string;
  modelEnabled: boolean;
  orchestration: {
    proposerModel: string | null;
    reviewerModel: string | null;
    independentReviewerConfigured: boolean;
    policies: Array<{
      id: string;
      name: string;
      cloudAllowed: boolean;
      redactionRequired: boolean;
      maxEvidenceRows: number;
      risk: "low" | "medium" | "high";
      release: string;
      mayWriteAmounts: false;
      mayWriteLedger: false;
    }>;
  };
  tasks: CapabilityTask[];
  learning: {
    suggestionCount: number;
    reviewedCount: number;
    correctionCount: number;
    evidenceGuardedCount: number;
    promotionEligible: boolean;
    promotionReason: string;
    latestEvaluation: null | {
      proposedLevel: "L0" | "L1" | "L2";
      category: string;
      modelVersion: string;
      evaluatedAt: string;
      metrics: Record<string, number | string>;
      reasons: string[];
    };
  };
}

export interface ProgressData {
  shop: string | null;
  period: string | null;
  platform?: string | null;
  periodState: "open" | "preclosed" | "closed" | "restated" | null;
  gates: Gate[];
  sourceCount: number;
  unresolvedCount: number;
  unexplainedAmount: string;
  taskSummary?: ProgressTaskSummary;
  currentTasks?: ProgressTask[];
  compute?: ComputeProgress;
}

export interface ProgressTaskSummary {
  total: number;
  waiting: number;
  running: number;
  succeeded: number;
  failed: number;
}

export interface ProgressTask {
  id?: string;
  label?: string | null;
  storeId?: string | null;
  platform?: string | null;
  shop?: string | null;
  period?: string | null;
  state?: "waiting" | "running" | "succeeded" | "failed" | string;
  progressPercent?: number;
  detail?: string | null;
  updatedAt?: string | null;
}

export interface ComputeProgress {
  enabled: boolean;
  running: boolean;
  cycleId: string | null;
  total: number;
  queued: number;
  active: number;
  succeeded: number;
  failed: number;
}

export interface ComputeJob {
  jobId: string;
  cycleId: string;
  kind: string;
  storeId: string | null;
  period: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  progressPercent: number;
  label: string;
  detail: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
}

export interface ComputeTarget {
  target_key: string;
  platform: string;
  logical_store: string;
  logical_store_key: string;
  period: string;
  status: "available" | "missing" | "partial" | string;
  period_state: "closed" | "partial" | string;
  source_ids: string[];
  evidence: string[];
  aliases: string[];
}

export interface ComputeTargetPlan {
  scope_start: string;
  scope_end: string;
  targets: ComputeTarget[];
  review_required: Array<{
    candidate: string;
    source_id: string;
    platform: string | null;
    reason: string;
    explanation: string;
  }>;
}

export interface ComputeRunResult {
  accepted: boolean;
  running: boolean;
  message: string;
}

export interface BalanceRow {
  balanceId: string;
  balanceKey: string;
  expectedAmount: string;
  actualAmount: string;
  matchedAmount: string;
  differenceAmount: string;
  status: "balanced" | "partial" | "unresolved";
}

export interface ReviewItem {
  unresolvedId: string;
  reasonCode: string;
  amount: string;
  status: string;
  businessTitle: string;
  businessSummary: string;
  suggestedAction: string;
  evidenceCount: number;
  storeId: string;
  storeName: string;
  period: string;
}

export interface ReviewPage {
  total: number;
  offset: number;
  limit: number;
  items: ReviewItem[];
  hasMore: boolean;
}

export interface ReviewGroup {
  groupId: string;
  storeId: string;
  storeName: string;
  period: string;
  reasonCode: string;
  businessTitle: string;
  businessSummary: string;
  suggestedAction: string;
  itemCount: number;
  totalAmount: string;
  absoluteAmount: string;
  evidenceCount: number;
}

export interface ReviewGroupPage {
  groupCount: number;
  recordCount: number;
  groups: ReviewGroup[];
}

export interface EvidenceSourceLine {
  snapshotId: string;
  artifactId: string | null;
  originalName: string;
  sourceMember: string | null;
  sourceSheet: string | null;
  rowNumber: number;
  field: string | null;
  normalizedValue: string | null;
  normalizationVersion: string | null;
  ruleVersionId: string | null;
  sourceKind: string | null;
}

export interface ReviewEvidenceDetail {
  unresolvedId: string;
  balanceId: string;
  lineageStatus: "frozen" | "legacy_inferred" | "unavailable";
  sources: EvidenceSourceLine[];
}

export interface EvidencePreviewCell {
  value: string | null;
  valueKind:
    | "blank"
    | "boolean"
    | "date"
    | "datetime"
    | "error"
    | "formula"
    | "number"
    | "text"
    | "time";
  formula: string | null;
  deterministic: boolean;
}

export interface ReviewEvidencePreview {
  unresolvedId: string;
  snapshotId: string;
  lineageStatus: "frozen";
  contentSha256: string;
  fileKind: "csv" | "xlsx";
  memberName: string | null;
  sheetNames: string[];
  originalName: string;
  readOnly: true;
  formulasAreDeterministic: false;
  sheet: {
    name: string;
    hidden: boolean;
    window: {
      headerRowNumber: number;
      targetRowNumber: number;
      targetDataRowNumber: number;
      startRowNumber: number;
      endRowNumber: number;
      targetColumnIndex: number | null;
      columns: Array<{
        index: number;
        label: string;
        sourceLabel: string;
      }>;
      rows: Array<{
        sourceRowNumber: number;
        sourceEndRowNumber: number;
        dataRowNumber: number;
        cells: EvidencePreviewCell[];
      }>;
    };
  };
  context: {
    storeName: string;
    period: string;
    businessTitle: string;
    whatHappened: string;
    whatItAffects: string;
    suggestedAction: string;
  };
  comparison: {
    businessKey: string;
    expectedAmount: string;
    actualAmount: string;
    matchedAmount: string;
    differenceAmount: string;
  };
  trace: Array<{
    label: string;
    detail: string;
  }>;
  sourceField: string | null;
  sourceValue: string | null;
}

export type TrustCellStatus =
  | "usable"
  | "missing_sources"
  | "amount_mismatch"
  | "waiting_review"
  | "processing"
  | "collecting";

export interface TrustCheck {
  key: "sources" | "amounts" | "trace" | "confirmation";
  label: string;
  state: "passed" | "failed" | "pending" | "not_applicable";
  explanation: string;
}

export interface TrustCell {
  periodId: string;
  storeId: string;
  storeName: string;
  platformId: string;
  period: string;
  status: TrustCellStatus;
  statusLabel: string;
  runId: string | null;
  firstReviewId: string | null;
  periodState: "open" | "preclosed" | "closed" | "restated";
  facts: {
    requiredSourceCount: number;
    presentSourceCount: number;
    missingSourceCount: number;
    failedSourceCount: number;
    unresolvedCount: number;
    unresolvedAmount: string;
    balanceCount: number;
    balancedCount: number;
    amountMatchRate: string;
    candidateInputCount: number;
    lastCalculatedAt: string | null;
  };
  explanation: {
    happened: string;
    impact: string;
    action: string;
    outcome: string;
  };
  checks: TrustCheck[];
}

export interface TrustMatrix {
  currentPeriod: string | null;
  periods: string[];
  stores: Array<{
    id: string;
    name: string;
    platformId: string;
  }>;
  cells: TrustCell[];
  summary: {
    storeCount: number;
    usableCount: number;
    attentionCount: number;
    missingSourceCount: number;
    amountMismatchCount: number;
    waitingReviewCount: number;
    processingCount: number;
    collectingCount: number;
    verdict: string;
  };
  firstAttention: TrustCell | null;
  boundary: string;
}

export interface ReviewSuggestion {
  status: "suggestion";
  suggestion: string;
  model: string;
  requestId: string | null;
  suggestionId: string | null;
  evidenceGuard: "passed" | "failed" | "not_run" | string;
  reviewerModel: string | null;
  reviewerStatus:
    | "passed"
    | "failed"
    | "not_configured"
    | "not_run"
    | string;
  reviewerReason: string | null;
  mayWriteLedger: false;
  requiresHumanReview: true;
}

export interface PerformanceOverview {
  status: "reference_ready" | "waiting_sources";
  calculationMode: "single" | "combined";
  period: string | null;
  referenceOnly: boolean;
  certifiedPerformanceAvailable: boolean;
  engineGate?: {
    status: "waiting" | "waiting_for_certified_product_ledger" | "blocked" | "certified";
    message: string;
    code: string | null;
    details: Record<string, unknown>;
  };
  rowCount: number;
  storeCount: number;
  personCount: number;
  productCount: number;
  formulaPassCount: number;
  formulaPassRate: string;
  metrics: {
    collectedAmount: string;
    refundAmount: string;
    productCost: string;
    advertisingFee: string;
    storeProfit: string;
  };
  assignment: {
    activeCount: number;
    conflictCount: number;
    latestEffectiveDate: string | null;
    provisionalPersonCount: number;
  };
}

export interface PerformancePersonRow {
  personId: string;
  personName: string;
  storeName: string;
  productCount: number;
  collectedAmount: string;
  refundAmount: string;
  productCost: string;
  advertisingFee: string;
  storeProfit: string;
  failedFormulaRows: number;
}

export interface PerformancePeople {
  period: string | null;
  calculationMode: "single" | "combined";
  referenceOnly: boolean;
  rows: PerformancePersonRow[];
}

export interface BusinessDecision {
  decisionId: string;
  subjectKind: string;
  question: string;
  businessImpact: string;
  status: "pending" | "decided" | "superseded";
  answer: string | null;
  decidedBy: string | null;
  decidedAt: string | null;
}

export interface InputRevisionCandidate {
  revisionId: string;
  originalName: string;
  sourceLabel: string;
  status: string;
  reason: string;
  rowCount: number;
}

export interface InputRevisionGroup {
  groupId: string;
  period: string;
  sourceKind: string;
  label: string;
  candidates: InputRevisionCandidate[];
}

export interface DashboardData {
  status: WorkbenchStatus;
  progress: ProgressData;
  balances: BalanceRow[];
  reviews: ReviewItem[];
  reviewTotal: number;
  reviewGroups: ReviewGroup[];
  reviewGroupTotal: number;
  businessDecisions: BusinessDecision[];
  inputRevisionGroups: InputRevisionGroup[];
}

export interface AnalyticsStore {
  id: string;
  name: string;
  platformId?: string | null;
  platformName?: string | null;
}

export interface AnalyticsPlatform {
  id: string;
  name: string;
}

export interface AnalyticsPeriod {
  value: string;
  label: string;
}

export interface AnalyticsSelection {
  platformId?: string;
  storeId: string;
  period: string;
  fromDate: string | null;
  toDate: string | null;
}

export interface AnalyticsCoverage {
  status: "system_checked" | "review_required" | "no_data" | string;
  profitStatus:
    | "system_checked"
    | "historical_pending"
    | "review_required"
    | string;
  message: string;
}

export interface AnalyticsMetrics {
  orderGross: string;
  refunds: string;
  netSales: string;
  walletNet: string;
  orderCount: number;
  transactionCount: number;
  unresolvedAmount?: string;
}

export interface AnalyticsTrendPoint {
  date: string;
  orderGross: string;
  refunds: string;
  netSales: string;
  walletNet: string;
}

export interface AnalyticsStoreBreakdown extends AnalyticsMetrics {
  storeId: string;
  storeName: string;
}

export interface AnalyticsTransaction {
  occurredAt: string | null;
  storeId: string;
  storeName: string;
  sourceKind: string;
  sourceLabel: string;
  amount: string;
  direction: "income" | "expense" | "neutral";
  businessDescription: string;
  businessKey?: string | null;
}

export interface AnalyticsMonthlyPnl {
  period: string;
  storeId: string;
  storeName: string;
  status: string;
  sourceStatus?: string;
  metrics?: Record<string, string>;
  profit?: string;
  operatingProfit?: string;
}

export interface AnalyticsOverview {
  filters: {
    platforms?: AnalyticsPlatform[];
    stores: AnalyticsStore[];
    periods: AnalyticsPeriod[];
    dateRange: {
      min: string | null;
      max: string | null;
    };
  };
  selection: AnalyticsSelection;
  coverage: AnalyticsCoverage;
  metrics: AnalyticsMetrics;
  trend: AnalyticsTrendPoint[];
  storeBreakdown: AnalyticsStoreBreakdown[];
  transactions: AnalyticsTransaction[];
  monthlyPnl: AnalyticsMonthlyPnl[];
}

export interface AnalyticsCatalogStore extends AnalyticsStore {
  periods: string[];
  fileCount: number;
  processed: boolean;
}

export interface AnalyticsCatalog {
  allRecordCount: number;
  candidateRecordCount: number;
  discoveredStoreCount: number;
  processedStoreCount: number;
  platforms?: AnalyticsPlatform[];
  stores: AnalyticsCatalogStore[];
}

export interface AnalyticsQuery {
  platformId: string;
  storeId: string;
  period: string;
  fromDate: string;
  toDate: string;
  limit?: number;
}
