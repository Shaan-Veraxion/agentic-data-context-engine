"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { AppTabs } from "@/components/AppTabs";
import { api, type ReviewItem, type Suggestion } from "@/lib/api";

const LABEL: Record<string, string> = {
  approve: "Approve", reject: "Reject", provide_sql: "Provide SQL", accept_gap: "Accept as known gap",
  answer: "Answer", map: "Map to dataset", acknowledge: "Acknowledge", confirm: "Confirm",
  keep_confirmed: "Keep the confirmed value", accept_new: "Accept the new value",
};

const AUTO = "auto_reviewer";

function SuggestionBox({ item, onDone }: { item: ReviewItem; onDone: () => void }) {
  const s = item.detail.suggestion as Suggestion | undefined;
  const [error, setError] = useState<string | null>(null);
  if (!s) return null;
  async function apply() {
    if (!s?.decision) return;
    setError(null);
    try {
      await api.decide(item.id, s.decision, s.payload ?? {});
      onDone();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className={`suggest ${s.decision ? "" : "none"}`}>
      <div className="row">
        <span className="badge agent">suggested{s.by.startsWith("llm") ? " by the model" : " by rules"}</span>
        <strong>{s.decision ? LABEL[s.decision] ?? s.decision : "A person decides"}</strong>
        <span className={`badge ${s.confidence === "high" ? "ok" : s.confidence === "medium" ? "warn" : ""}`}>{s.confidence}</span>
        {s.decision && <button className="primary" onClick={apply}>Apply suggestion</button>}
      </div>
      <p className="muted" style={{ margin: "4px 0 0" }}>{s.reason}</p>
      {typeof s.payload?.sql === "string" && <pre className="code">{s.payload.sql}</pre>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Decide({ item, onDone }: { item: ReviewItem; onDone: () => void }) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const questions = (item.detail.questions as string[] | undefined) ?? [];
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ""));
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: string) {
    setError(null);
    const payload: Record<string, unknown> = {};
    if (decision === "provide_sql") payload.sql = text;
    if (decision === "answer" && item.kind === "gap") payload.answers = Object.fromEntries(questions.map((q, n) => [q, answers[n]]));
    else if (decision === "answer") Object.assign(payload, { value: text, ...(name ? { name } : {}) });
    if (decision === "map") payload.dataset = text;
    if (["reject", "keep_confirmed", "accept_new"].includes(decision) && text) payload.reason = text;
    try {
      await api.decide(item.id, decision, payload);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const gap = item.kind === "gap";
  const needsText = !gap && item.options.some((o) => ["provide_sql", "answer", "map"].includes(o));
  const wantsReason = item.options.some((o) => ["reject", "keep_confirmed"].includes(o));
  return (
    <div>
      {gap && questions.map((q, n) => (
        <div key={n} style={{ marginBottom: 6 }}>
          <div>{q}</div>
          <input style={{ width: "100%" }} value={answers[n]} placeholder="answer"
            onChange={(e) => setAnswers((a) => a.map((x, i) => (i === n ? e.target.value : x)))} />
        </div>
      ))}
      {item.kind === "unresolved_variable" && (
        <input placeholder="variable name" value={name} onChange={(e) => setName(e.target.value)} style={{ marginBottom: 6 }} />
      )}
      {(needsText || wantsReason) && (
        <textarea
          rows={item.options.includes("provide_sql") ? 4 : 1}
          placeholder={
            item.options.includes("provide_sql") ? "SELECT … FROM src  (src is the statement's input)"
              : item.options.includes("map") ? "Dremio dataset path, e.g. sales.erp.orders_v2"
              : item.options.includes("answer") ? "value" : "reason (optional)"
          }
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      )}
      <div className="row" style={{ marginTop: 6 }}>
        {item.options.map((o) => (
          <button key={o} onClick={() => decide(o)}>{LABEL[o] ?? o}</button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Detail({ item, appId }: { item: ReviewItem; appId: string }) {
  const d = item.detail as Record<string, unknown>;
  const table = item.subject.includes("/table:") ? item.subject.split("/table:")[1] : null;
  const tableLink = table && (
    <Link href={`/apps/${appId}/tables/${encodeURIComponent(table)}`}> see the SQL next to the script →</Link>
  );
  if (item.kind === "approve_sql") {
    const failed = (d.failed as unknown[] | undefined)?.length ?? 0;
    const down = Number(d.tables_downstream ?? 0);
    return (
      <p>
        Level <strong>{String(d.level)}</strong> in the {String(d.venue)} venue · {String(d.comparisons_ok)}/
        {String(d.comparisons)} comparisons match{failed ? ` · ${failed} differences` : ""}
        {down > 0 && <> · <strong>{down}</strong> tables downstream depend on it</>}
        {tableLink}
      </p>
    );
  }
  if (item.kind === "untranslated") {
    return <p>{String(d.span)} — {String(d.reason)} <span className="chip">{String(d.tier)}</span>{tableLink}</p>;
  }
  if (item.kind === "scope") {
    const files = (d.files as { file: string; class: string }[] | undefined) ?? [];
    return <p>{files.map((f) => <span key={f.file} className="chip" title={f.class}>{f.file} · {f.class}</span>)}</p>;
  }
  if (item.kind === "qvd_linkage") {
    return <p>The QVD header says it was created by <strong>{String(d.creator_doc)}</strong>{d.records != null && <> ({String(d.records)} rows)</>}.</p>;
  }
  if (item.kind === "conflict") {
    return <p>{String(d.reason)} <span className="muted">(facts {(d.facts as string[] | undefined)?.join(" vs ")})</span></p>;
  }
  if (item.kind === "gap") {
    return (
      <>
        <p>{String(d.summary ?? "")}{tableLink}</p>
        {typeof d.dremio_approach === "string" && d.dremio_approach && <p className="muted">In Dremio: {d.dremio_approach}</p>}
      </>
    );
  }
  if (typeof d.message === "string") return <p>{d.message}</p>;
  if (typeof d.note === "string") return <p className="muted">{d.note}</p>;
  return null;
}

function DecidedRow({ item, onDone }: { item: ReviewItem; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <tr>
      <td><span className="badge mode">{item.mode}</span></td>
      <td>{item.title}<div className="muted">{item.subject}</div></td>
      <td><strong>{LABEL[item.decision ?? ""] ?? item.decision}</strong></td>
      <td>{item.decided_by === AUTO ? <span className="badge agent">auto-reviewer</span> : item.decided_by}</td>
      <td>{item.decided_at ? new Date(item.decided_at).toLocaleString() : ""}</td>
      <td>
        <button onClick={() => api.reopen(item.id).then(onDone).catch((e: Error) => setError(e.message))}>Reopen</button>
        {error && <div className="error">{error}</div>}
      </td>
    </tr>
  );
}

export default function ReviewQueue({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = use(params);
  const [view, setView] = useState<"open" | "decided">("open");
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    api.review(appId, view).then(setItems).catch((e: Error) => setError(e.message));
  }, [appId, view]);
  useEffect(load, [load]);

  const withSuggestion = (items ?? []).filter((i) => (i.detail.suggestion as Suggestion | undefined)?.decision);
  async function applyAll() {
    for (const i of withSuggestion) {
      const s = i.detail.suggestion as Suggestion;
      if (s.confidence !== "high" || !s.decision) continue;
      try {
        await api.decide(i.id, s.decision, s.payload ?? {});
      } catch (e) {
        setError((e as Error).message);
      }
    }
    load();
  }
  const highCount = withSuggestion.filter((i) => (i.detail.suggestion as Suggestion).confidence === "high").length;

  return (
    <>
      <h1>{appId}</h1>
      <AppTabs app={appId} on="review" />
      <div className="row">
        <div className="seg">
          <button className={view === "open" ? "on" : ""} onClick={() => setView("open")}>Open</button>
          <button className={view === "decided" ? "on" : ""} onClick={() => setView("decided")}>Decided</button>
        </div>
        <button onClick={() => api.refreshReview(appId).then(load).catch((e: Error) => setError(e.message))}>Refresh from knowledge</button>
        {view === "open" && highCount > 0 && <button onClick={applyAll}>Apply {highCount} high-confidence suggestions</button>}
        <span className="muted">
          {view === "open" ? "Ordered by impact × uncertainty. Every decision is a confirmed fact the next run uses."
            : "Every decision can be reopened: its fact is invalidated and the item returns to the queue."}
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {!items ? (
        <p className="muted">Loading…</p>
      ) : !items.length ? (
        <div className="panel muted">{view === "open" ? "Nothing to review." : "No decisions yet."}</div>
      ) : view === "decided" ? (
        <div className="panel">
          <table className="grid">
            <thead><tr><th>Mode</th><th>Item</th><th>Decision</th><th>By</th><th>When</th><th></th></tr></thead>
            <tbody>{items.map((i) => <DecidedRow key={i.id} item={i} onDone={load} />)}</tbody>
          </table>
        </div>
      ) : (
        items.map((i) => (
          <div key={i.id} className="panel">
            <div className="row">
              <span className="badge mode">{i.mode}</span>
              <strong>{i.title}</strong>
              <span className="muted">score {i.score}</span>
            </div>
            <p className="muted" style={{ margin: "6px 0" }}>{i.subject}</p>
            <Detail item={i} appId={appId} />
            <SuggestionBox item={i} onDone={load} />
            <Decide item={i} onDone={load} />
          </div>
        ))
      )}
    </>
  );
}
