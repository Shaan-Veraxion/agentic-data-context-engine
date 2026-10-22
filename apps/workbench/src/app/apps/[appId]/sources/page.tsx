"use client";

import { use, useCallback, useEffect, useState } from "react";
import { AppTabs } from "@/components/AppTabs";
import { api, type SourceRow } from "@/lib/api";

export default function Sources({ params }: { params: Promise<{ appId: string }> }) {
  const { appId } = use(params);
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    api.sources(appId).then(setRows).catch((e: Error) => setError(e.message));
  }, [appId]);
  useEffect(load, [load]);

  const changed = Object.entries(edits).filter(([, v]) => v.trim());
  async function save() {
    setError(null);
    try {
      const r = await api.mapSources(appId, Object.fromEntries(changed));
      setMsg(`${Object.keys(r.mapped).length} sources mapped. Run the application again to validate with them.`);
      setEdits({});
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const shown = (rows ?? []).filter((r) => r.source.toLowerCase().includes(filter.toLowerCase()));
  const unmapped = (rows ?? []).filter((r) => !r.dataset).length;

  return (
    <>
      <h1>{appId}</h1>
      <AppTabs app={appId} on="sources" />
      <p className="muted">
        Every source the application&apos;s SQL reads. Map each one to its Dremio dataset; a mapping is a confirmed fact
        (it also answers the matching item in the review queue).
      </p>
      <div className="row">
        <input placeholder="filter" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <span className="muted">{rows ? `${rows.length} sources · ${unmapped} not mapped` : ""}</span>
        <button className="primary" disabled={!changed.length} onClick={save}>Save {changed.length || ""} mappings</button>
      </div>
      {msg && <p className="ok-text">{msg}</p>}
      {error && <p className="error">{error}</p>}
      {!rows ? (
        <p className="muted">Loading…</p>
      ) : !rows.length ? (
        <div className="panel muted">No sources yet: run the application first.</div>
      ) : (
        <div className="panel">
          <table className="grid">
            <thead><tr><th>Source</th><th>Dremio dataset</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.source}>
                  <td><code>{r.source}</code></td>
                  <td>
                    <input style={{ width: "100%" }} placeholder={r.dataset ?? "space.folder.dataset"}
                      value={edits[r.source] ?? r.dataset ?? ""}
                      onChange={(e) => setEdits((x) => ({ ...x, [r.source]: e.target.value }))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
