import Link from "next/link";

export function AppTabs({ app, on }: { app: string; on: "overview" | "review" | "runs" | "sources" | "lineage" | "table" }) {
  const tab = (key: typeof on, href: string, label: string) => (
    <Link href={href} className={on === key ? "on" : ""}>
      {label}
    </Link>
  );
  return (
    <nav className="tabs">
      {tab("overview", `/apps/${app}`, "Overview")}
      {tab("review", `/apps/${app}/review`, "Review queue")}
      {tab("runs", `/apps/${app}/runs`, "Runs")}
      {tab("sources", `/apps/${app}/sources`, "Sources")}
      {tab("lineage", `/apps/${app}/lineage`, "Lineage")}
    </nav>
  );
}

export function LevelBadge({ level, venue }: { level: string | null | undefined; venue?: string | null }) {
  if (!level) return <span className="badge">not run</span>;
  const n = Number(level.replace("V", ""));
  const cls = Number.isNaN(n) ? "bad" : n >= 3 ? "ok" : n >= 1 ? "warn" : "bad";
  return (
    <span className={`badge ${cls}`} title={venue === "local" ? "validated in the local venue (DuckDB); Dremio re-validation in phase PD" : venue ?? ""}>
      {level}
      {venue ? ` · ${venue}` : ""}
    </span>
  );
}
