"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { AppTabs, LevelBadge } from "@/components/AppTabs";
import { api, type TableDetail, type TableStep } from "@/lib/api";

function Step({ appId, table, step, onSaved }: { appId: string; table: string; step: TableStep; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [sql, setSql] = useState(step.sql);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setError(null);
    try {
      await api.statementSql(appId, {
        table, statement: step.statements[0], span: step.spans[0], sql, text: step.text, input_columns: step.input_columns,
      });
      setMsg("Saved as a confirmed correction. The next run uses it instead of the rules.");
      setEditing(false);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="panel">
      <div className="row">
        <strong>{step.cte}</strong>
        <span className="chip">{step.kind}</span>
        {step.statements.length > 0 && <span className="muted">statements {step.statements.join(", ")}</span>}
        {step.ai && <span className="badge warn">AI-assisted · needs review</span>}
        <span className="muted">{step.spans.join(", ")}</span>
        {step.spans.length > 0 && step.statements.length > 0 && !editing && (
          <button onClick={() => setEditing(true)}>Correct SQL</button>
        )}
      </div>
      <div className="split">
        <div>
          <div className="muted small">Qlik script</div>
          <pre className="code">{step.script || "(no script lines)"}</pre>
        </div>
        <div>
          <div className="muted small">SQL{editing ? " — reads the step's input as src" : ""}</div>
          {editing ? (
            <>
              <textarea rows={Math.max(6, sql.split("\n").length + 1)} value={sql} onChange={(e) => setSql(e.target.value)} />
              {step.input_columns && <div className="muted small">src columns: {step.input_columns.join(", ")}</div>}
              <div className="row" style={{ marginTop: 6 }}>
                <button className="primary" onClick={save}>Save correction</button>
                <button onClick={() => { setEditing(false); setSql(step.sql); }}>Cancel</button>
              </div>
            </>
          ) : (
            <pre className="code">{step.sql}</pre>
          )}
        </div>
      </div>
      {msg && <p className="ok-text">{msg}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

export default function TablePage({ params }: { params: Promise<{ appId: string; table: string }> }) {
  const { appId, table: raw } = use(params);
  const table = decodeURIComponent(raw);
  const [data, setData] = useState<TableDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [full, setFull] = useState(false);

  const load = useCallback(() => {
    api.table(appId, table).then(setData).catch((e: Error) => setError(e.message));
  }, [appId, table]);
  useEffect(load, [load]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const v = data.validation as Record<string, unknown> | null;
  const failed = (v?.failed as { check?: string; detail?: string }[] | undefined) ?? [];

  return (
    <>
      <h1>{appId} · {table}</h1>
      <AppTabs app={appId} on="table" />
      <p><Link href={`/apps/${appId}`}>← all tables</Link></p>
      <div className="row">
        <div className="panel">
          <div className="muted">Validation</div>
          <LevelBadge level={(v?.level as string) ?? null} venue={(v?.venue as string) ?? null} />
          {v && <span className="muted"> {String(v.comparisons_ok)}/{String(v.comparisons)} comparisons match</span>}
        </div>
        <div className="panel">
          <div className="muted">Steps</div>
          <strong>{data.steps.length}</strong>
        </div>
        <div className="panel">
          <div className="muted">Not translated</div>
          <strong>{data.untranslated.length}</strong>
        </div>
      </div>

      {failed.length > 0 && (
        <div className="panel">
          <strong>Comparisons that fail</strong>
          <ul>{failed.map((f, n) => <li key={n}>{f.check ?? ""} {f.detail ?? JSON.stringify(f)}</li>)}</ul>
        </div>
      )}

      {data.untranslated.length > 0 && (
        <>
          <h2>Not translated</h2>
          {data.untranslated.map((u) => (
            <div key={u.statement} className="panel">
              <div className="row"><strong>statement {u.statement}</strong><span className="muted">{u.span}</span><span className="chip">{u.tier}</span></div>
              <p>{u.reason}</p>
              <pre className="code">{u.text}</pre>
              <Link href={`/apps/${appId}/review`}>Provide its SQL in the review queue →</Link>
            </div>
          ))}
        </>
      )}

      <h2>Step by step</h2>
      <p className="muted">Each step of the SQL next to the script lines it came from. Correct any step: the correction is a confirmed fact, and the next run uses it.</p>
      {data.steps.map((s) => <Step key={s.cte} appId={appId} table={table} step={s} onSaved={load} />)}

      {data.notes.length > 0 && (
        <div className="panel"><strong>Notes</strong><ul>{data.notes.map((n, i) => <li key={i}>{n}</li>)}</ul></div>
      )}

      <h2>Full SQL</h2>
      <button onClick={() => setFull((f) => !f)}>{full ? "Hide" : "Show"} the whole query</button>
      {full && <pre className="code">{data.sql ?? "(not complete: see what is not translated)"}</pre>}
    </>
  );
}
