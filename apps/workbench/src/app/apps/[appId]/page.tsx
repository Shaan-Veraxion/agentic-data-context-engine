"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { AppTabs, LevelBadge } from "@/components/AppTabs";
import { api, type AutoReview, type Overview } from "@/lib/api";

export default function AppOverview({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = use(params);
  const [data, setData] = useState<Overview | null>(null);
  const [auto, setAuto] = useState<AutoReview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.overview(appId).then(setData).catch((e: Error) => setError(e.message));
    api.autoReview(appId).then(setAuto).catch(() => setAuto(null));
  }, [appId]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const pack = data.latest_pack;

  return (
    <>
      <h1>{appId}</h1>
      <AppTabs app={appId} on="overview" />
      <div className="row">
        <div className="panel">
          <div className="muted">Statements understood</div>
          <strong>{data.coverage.understood} / {data.coverage.total}</strong> ({Math.round(data.coverage.ratio * 100)}%)
        </div>
        <div className="panel">
          <div className="muted">Open review items</div>
          <strong>{data.open_review_items}</strong>
        </div>
        <div className="panel">
          <div className="muted">Issues found in the script</div>
          <strong>{data.issues.length}</strong>
        </div>
        {auto && (
          <div className="panel" title="Facts the trust gate routed auto (validated by data, confident, low impact) and items that only inform. Reopen any of them in the review queue.">
            <div className="muted">Auto-reviewer</div>
            <strong>{auto.accepted.length}</strong> accepted · <strong>{auto.decided_items.length}</strong> acknowledged
            <div className={auto.stats.reversal_rate > auto.stats.target ? "error small" : "muted small"}>
              reversed by reviewers: {auto.stats.reversed} ({Math.round(auto.stats.reversal_rate * 100)}%, target &lt; {Math.round(auto.stats.target * 100)}%)
            </div>
          </div>
        )}
      </div>

      <h2>Tables</h2>
      <div className="panel">
        <table className="grid">
          <thead>
            <tr><th>Table</th><th>Level</th><th>Comparisons</th><th>Checks</th><th>Approved</th><th>Fields</th></tr>
          </thead>
          <tbody>
            {data.tables.map((t) => (
              <tr key={t.key}>
                <td><Link href={`/apps/${appId}/tables/${encodeURIComponent(t.name)}`}>{t.name}</Link>{t.dropped && <span className="muted"> (dropped)</span>}</td>
                <td><LevelBadge level={t.level} venue={t.venue} /></td>
                <td>{t.comparisons ?? "–"}</td>
                <td>{t.checks_ok === null ? "–" : t.checks_ok ? "pass" : "fail"}</td>
                <td>{t.approved ? "yes" : "no"}</td>
                <td>{t.fields.map((f) => <span key={f} className="chip">{f}</span>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pack && (
        <>
          <h2>Latest migration pack</h2>
          <div className="panel">
            <span className="muted">Run {pack.run_id}: </span>
            {pack.files.map((f) => (
              <a key={f} className="chip" href={api.packFileUrl(appId, pack.run_id, f)} target="_blank" rel="noreferrer">{f}</a>
            ))}
          </div>
        </>
      )}

      {data.issues.length > 0 && (
        <>
          <h2>Issues</h2>
          <div className="panel">
            <table className="grid">
              <tbody>
                {data.issues.map((i, n) => (
                  <tr key={n}><td><span className="chip">{i.code}</span></td><td>{i.message}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
