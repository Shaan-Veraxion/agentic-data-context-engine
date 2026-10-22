"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type AppSummary } from "@/lib/api";

export default function Home() {
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [appId, setAppId] = useState("");
  const [appName, setAppName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => api.apps().then(setApps).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  async function submit() {
    setError(null);
    try {
      if (files.length) {
        setBusy("Uploading…");
        await api.upload(appId, files);
      }
      setBusy("Running the pipeline…");
      const run = await api.run(appId, appName);
      setBusy(`Run ${run.run_id}: ${run.status} · ${Object.entries(run.levels ?? {}).map(([t, l]) => `${t} ${l}`).join(", ")}`);
      await load();
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  }

  return (
    <>
      <h1>Applications</h1>
      <p className="muted">Qlik applications read from their scripts, translated to Dremio SQL and validated.</p>

      <div className="panel">
        <table className="grid">
          <thead>
            <tr><th>Application</th><th>Statements</th><th>Coverage</th><th>Unresolved variables</th></tr>
          </thead>
          <tbody>
            {apps.map((a) => (
              <tr key={a.app_id}>
                <td><Link href={`/apps/${a.app_id}`}>{a.name}</Link> <span className="muted">({a.app_id})</span></td>
                <td>{a.coverage.total}</td>
                <td>{Math.round(a.coverage.ratio * 100)}%</td>
                <td>{a.coverage.unresolved_variables}</td>
              </tr>
            ))}
            {!apps.length && (
              <tr><td colSpan={4} className="muted">No applications yet. Upload scripts below.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <h2>Upload files and run</h2>
      <div className="panel">
        <p className="muted">
          Scripts (.qvs), reload logs, QVD files, and optionally local source extracts in a folder named
          <code> sources/&lt;space&gt;/&lt;…&gt;/&lt;table&gt;.csv</code> (used until Dremio is connected).
        </p>
        <div className="row">
          <input placeholder="app id (e.g. sales_overview)" value={appId} onChange={(e) => setAppId(e.target.value)} />
          <input placeholder="display name (optional)" value={appName} onChange={(e) => setAppName(e.target.value)} />
          <input type="file" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <button className="primary" disabled={!/^[A-Za-z0-9_-]{1,64}$/.test(appId) || !!busy?.endsWith("…")} onClick={submit}>
            Upload and run
          </button>
        </div>
        {busy && <p>{busy}</p>}
        {error && <p className="error">{error}</p>}
      </div>
    </>
  );
}
