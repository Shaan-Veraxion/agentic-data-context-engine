# Architecture: version archive

Every version of `docs/architecture.md` is kept here, unchanged. **Nothing in this folder is ever deleted or edited.** The current version lives only at [`docs/architecture.md`](../../architecture.md).

| Version | File | Date | What changed |
|---|---|---|---|
| v0.1 | [architecture-v0.1.md](architecture-v0.1.md) | 2026-10-08 | First analysis of the proposal. Reduced scope (about 12 LLM agents plus deterministic services, 2 engineers). Superseded. |
| v0.2 | [architecture-v0.2.md](architecture-v0.2.md) | 2026-10-08 | Full proposal scope restored (26 agents, all stores, Keycloak); parallel squads with frozen contracts. |
| v0.3 | [architecture-v0.3.md](architecture-v0.3.md) | 2026-10-08 | CP-001 merged (BI adapters, Qlik first; two-mode Reverse-SQL; 29 agents). ADR-12: reuse Brahmastra behind a port. |
| v0.4 | [architecture-v0.4.md](architecture-v0.4.md) | 2026-10-08 | ADR-12 fit assessment (R1–R15) from Brahmastra docs; fact log as source of truth; gateway memoization; QlikView + Qlik Sense scope. |
| v0.5 | [architecture-v0.5.md](architecture-v0.5.md) | 2026-10-08 | ADR-12 revised: Brahmastra evolved in place as `packages/kg-engine` (core/apps split, no wrapper layer). |
| v0.6 | [architecture-v0.6.md](architecture-v0.6.md) | 2026-10-08 | Qlik scripts first (Mode A1/A2); ADR-13 Dremio as data source; ADR-14 Google Vertex AI; ADR-15 local → production, shippable; open questions moved to `open-questions.md`. |
| v0.7 | [architecture-v0.7.md](architecture-v0.7.md) | 2026-10-08 | ADR-16: messy Dremio estate (equivalence clusters, dataset roles, gated golden records); agents #30–31 (31 agents). |
| v0.8 | (current) | 2026-10-09 | ADR-17: Data model designer (agent #32, later stage); star schema / normalized model / curated Dremio views; Qlik not re-pointed. |

## How these files were produced

The repository had no version control while v0.1–v0.7 were written, and the document was edited in place.

- **v0.7** is a direct copy of the current document.
- **v0.1 and v0.2** were rebuilt from the full text written in the working session.
- **v0.3 to v0.6** were rebuilt by undoing the session's recorded edits in reverse order, starting from v0.7. Each undo step was checked to match exactly once.
- The rebuilt v0.3 (602 lines) and v0.6 (775 lines) match the line counts recorded when those versions were first written.

## Rule from now on

Whenever `docs/architecture.md` gets a new version number, the previous version is copied here first as `architecture-vX.Y.md` and a row is added to the table above. Once the repository is under git, git history is the primary record and these snapshots stay as a readable archive.
