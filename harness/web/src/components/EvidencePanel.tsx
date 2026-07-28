import type { ReviewEvidenceDetail } from "../types";

function sourceLocation(
  member: string | null,
  sheet: string | null,
  rowNumber: number
): string {
  const parts = [
    member ? `压缩包内：${member}` : null,
    sheet ? `工作表：${sheet}` : null,
    `第 ${rowNumber.toLocaleString("zh-CN")} 行`
  ].filter((value): value is string => Boolean(value));
  return parts.join(" · ");
}

export function EvidencePanel({ detail }: { detail: ReviewEvidenceDetail }) {
  if (detail.sources.length === 0) {
    return (
      <div className="evidence-empty">
        这条差额尚未形成可展示的原始行定位，系统不会用推测替代证据。
      </div>
    );
  }

  return (
    <section aria-label="原始依据" className="evidence-panel">
      <header>
        <div>
          <h4>原始依据</h4>
          <p>
            {detail.lineageStatus === "frozen"
              ? "以下位置在本次核对时已冻结。"
              : "以下位置由旧版记录恢复；重新核对后会形成完整冻结证据。"}
          </p>
        </div>
        <span>{detail.sources.length.toLocaleString("zh-CN")} 行</span>
      </header>
      <ol className="evidence-list">
        {detail.sources.map((source, index) => (
          <li
            key={`${source.snapshotId}-${source.rowNumber}-${source.field ?? ""}-${index}`}
          >
            <div className="evidence-file">
              <strong>{source.originalName || "未命名来源文件"}</strong>
              <span>
                {sourceLocation(
                  source.sourceMember,
                  source.sourceSheet,
                  source.rowNumber
                )}
              </span>
            </div>
            <dl>
              <div>
                <dt>核对字段</dt>
                <dd>{source.field || "金额"}</dd>
              </div>
              <div>
                <dt>标准化值</dt>
                <dd>{source.normalizedValue ?? "未记录"}</dd>
              </div>
              <div>
                <dt>处理版本</dt>
                <dd>{source.ruleVersionId || source.normalizationVersion || "旧版记录"}</dd>
              </div>
            </dl>
          </li>
        ))}
      </ol>
    </section>
  );
}
