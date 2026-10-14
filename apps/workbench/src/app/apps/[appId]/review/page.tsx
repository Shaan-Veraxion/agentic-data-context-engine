"use client";

import { use, useCallback, useEffect, useState } from "react";
import { AppTabs } from "@/components/AppTabs";
import { api, type ReviewItem } from "@/lib/api";

const LABEL: Record<string, string> = {
  approve: "Approve", reject: "Reject", provide_sql: "Provide SQL", accept_gap: "Accept as known gap",
  answer: "Answer", map: "Map to dataset", acknowledge: "Acknowledge",
};

function Decide({ item, onDone }: { item: ReviewItem; onDone: () => void }) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: string) {
    setError(null);
    const payload: Record<string, unknown> = {};
    if (decision === "provide_sql") payload.sql = text;
    if (decision === "answer") Object.assign(payload, { value: text, ...(name ? { name } : {}) });
    if (decision === "map") payload.dataset = text;
    if (decision === "reject" && text) payload.reason = text;
    try {
      await api.decide(item.id, decision, payload);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const needsText = item.options.some((o) => ["provide_sql", "answer", "map"].includes(o));
  return (
    <div>
      {item.kind === "unresolved_variable" && (
        <input placeholder="variable name" value={name} onChange={(e) => setName(e.target.value)} style={{ marginBottom: 6 }} />
      )}
      {(needsText || item.options.includes("reject")) && (
        <textarea
          rows={item.options.includes("provide_sql") ? 4 : 1}
          placeholder={
            item.options.includes("provide_sql") ? 'SELECT … FROM src  (src is the statement\'s input)'
              : item.options.includes("map") ? "Dremio dataset path, e.g. sales.erp.orders_v2"
              : item.options.includes("answer") ? "value" : "reason (optional)"
          }
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      )}
      <div className="row" style={{ marginTop: 6 }}>
        {item.options.map((o) => (
          <button key={o} className={o === "approve" ? "primary" : ""} onClick={() => decide(o)}>{LABEL[o] ?? o}</button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Detail({ item }: { item: ReviewItem }) {
  const d = item.detail as Record<string, unknown>;
  if (item.kind === "approve_sql") {
    const failed = (d.failed as unknown[] | undefined)?.length ?? 0;
    return (
      <p>
        Level <strong>{String(d.level)}</strong> in the {String(d.venue)} venue · {String(d.comparisons_ok)}/
        {String(d.comparisons)} comparisons match{failed ? ` · ${failed} differences` : ""}
      </p>
    );
  }
  if (item.kind === "untranslated") {
    return <p>{String(d.span)} — {String(d.reason)} <span className="chip">{String(d.tier)}</span></p>;
  }
  if (typeof d.message === "string") return <p>{d.message}</p>;
  if (typeof d.note === "string") return <p className="muted">{d.note}</p>;
  return null;
}

export default function ReviewQueue({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = use(params);
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.review(appId).then(setItems).catch((e: Error) => setError(e.message));
  }, [appId]);
  useEffect(load, [load]);

  return (
    <>
      <h1>{appId}</h1>
      <AppTabs app={appId} on="review" />
      <div className="row">
        <span className="muted">Ordered by impact × uncertainty. Decisions are recorded as confirmed facts and used by the next run.</span>
        <button onClick={() => api.refreshReview(appId).then(load)}>Refresh from knowledge</button>
      </div>
      {error && <p className="error">{error}</p>}
      {!items ? (
        <p className="muted">Loading…</p>
      ) : !items.length ? (
        <div className="panel muted">Nothing to review.</div>
      ) : (
        items.map((i) => (
          <div key={i.id} className="panel">
            <div className="row">
              <span className="badge mode">{i.mode}</span>
              <strong>{i.title}</strong>
              <span className="muted">score {i.score}</span>
            </div>
            <p className="muted" style={{ margin: "6px 0" }}>{i.subject}</p>
            <Detail item={i} />
            <Decide item={i} onDone={load} />
          </div>
        ))
      )}
    </>
  );
}
