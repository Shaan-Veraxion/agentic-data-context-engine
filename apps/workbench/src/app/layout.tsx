import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "ADCE Workbench",
  description: "Review Qlik-to-SQL reconstruction: coverage, validation levels, review queue and lineage.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <strong>ADCE Workbench</strong>
          <Link href="/">Applications</Link>
          <span className="muted">Qlik → Dremio SQL · starting stage</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
