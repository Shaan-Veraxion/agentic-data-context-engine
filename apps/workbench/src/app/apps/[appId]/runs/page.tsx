"use client";

import { use, useCallback, useEffect, useState } from "react";
import { AppTabs, LevelBadge } from "@/components/AppTabs";
import { api, type ReviewItem, type RunDetail, type RunResponse, type RunRow } from "@/lib/api";

function when(t: number | null | undefined) {
  return t ? new Date(t * 1000).toLocaleString() : "";
}

function Timeline({ run }: { run: RunDetail }) {
  const trace = run.trace ?? [];
  const t0 = trace[0]?.at ?? 0;
  return (
    <table className="grid">
      <thead><tr><th>+s</th><th>Node</th><th>What happened</th></tr></thead>
      <tbody>
        {trace.map((e, n) => {
          const { node, at, ...rest } = e;
          const sup = node === "supervisor";
          return (
            <tr key={n} className={sup ? "sup" : ""}>
              <td className="muted">{(at - t0).toFixed(1)}</td>
              <td><span className={`chip ${sup ? "agent" : ""}`}>{node}</span></td>
              <td>
                {sup ? (
                  <>
                    <strong>{String(rest.action)}{rest.table ? ` ${String(rest.table)}` : ""}</strong>
                    <span className="muted"> — {String(rest.reason ?? "")} ({String(rest.by ?? "")})</span>
                    {Array.isArray(rest.options) && <div className="muted small">options: {(rest.options as string[]).join(", ")}</div>}
                  </>
                ) : (
                  <span className="muted small">{Object.entries(rest).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ")}</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Answers({ appId, run, onDone }: { appId: string; run: RunResponse; onDone: (r: RunResponse) => void }) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.review(appId).then((xs) => setItems(xs.filter((i) => i.kind === "unresolved_variable"))).catch(() => setItems([]));
  }, [appId]);
  async function send() {
    try {
      onDone(await api.answerRun(appId, run.run_id, values));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="panel">
      <strong>The run is waiting for the owner</strong>
      <ul>{(run.questions ?? []).map((q, n) => <li key={n}>{q}</li>)}</ul>
      {items.map((i) => (
        <div key={i.id} className="row" style={{ marginBottom: 6 }}>
          <span style={{ minWidth: 280 }}>{i.title}</span>
          <input placeholder="value" value={values[i.id] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [i.id]: e.target.value }))} />
        </div>
      ))}
      <button className="primary" disabled={!Object.values(values).some((v) => v)} onClick={send}>Answer and continue the run</button>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

export default function Runs({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = use(params);
  const [rows, setRows] = useState<RunRow[] | null>(null);
  const [open, setOpen] = useState<RunDetail | null>(null);
  const [last, setLast] = useState<RunResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.runs(appId).then(setRows).catch((e: Error) => setError(e.message));
  }, [appId]);
  useEffect(load, [load]);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.run(appId);
      setLast(r);
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function show(id: string) {
    try {
      setOpen(await api.getRun(appId, id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <>
      <h1>{appId}</h1>
      <AppTabs app={appId} on="runs" />
      <div className="row">
        <button className="primary" disabled={busy} onClick={start}>{busy ? "Running…" : "Run again"}</button>
        <span className="muted">A run re-reads the knowledge: decisions made in the review queue are used, and only the tables they affect (and those downstream) are validated again.</span>
      </div>
      {error && <p className="error">{error}</p>}
      {last && last.status === "waiting_for_owner" && <Answers appId={appId} run={last} onDone={(r) => { setLast(r); load(); }} />}
      {last && last.status === "queued" && <div className="panel">Queued (request {String(last.request_id)}); the worker runs it. Refresh to see it.</div>}

      {!rows ? (
        <p className="muted">Loading…</p>
      ) : !rows.length ? (
        <div className="panel muted">No finished runs yet.</div>
      ) : (
        <div className="panel">
          <table className="grid">
            <thead><tr><th>Run</th><th>Started</th><th>Status</th><th>Scope</th><th>Supervisor decisions</th><th>Levels</th><th></th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.run_id}>
                  <td><code>{r.run_id}</code></td>
                  <td>{when(r.started)}</td>
                  <td>{r.status}</td>
                  <td>{r.scope_tables ? r.scope_tables.join(", ") || "none" : "all tables"}</td>
                  <td>{r.decisions}</td>
                  <td>{Object.entries(r.levels).map(([t, l]) => <span key={t} className="chip">{t} <LevelBadge level={l} /></span>)}</td>
                  <td><button onClick={() => show(r.run_id)}>Timeline</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <>
          <h2>Run {open.run_id}</h2>
          <p className="muted">Every node the graph ran, in order. Supervisor rows show what it chose, why, and what else it could have done.</p>
          <div className="panel"><Timeline run={open} /></div>
        </>
      )}
    </>
  );
}
