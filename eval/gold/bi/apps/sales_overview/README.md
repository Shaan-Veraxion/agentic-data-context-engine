# Fixture app: sales_overview

A synthetic Qlik Sense app, based on the worked example in the architecture-and-workflow proposal. It is safe to keep in git.

| File | What it is |
|---|---|
| `script.qvs` | The load script. It covers labels, a preceding LOAD over SQL, MAPPING LOAD INLINE, ApplyMap, variables (SET, literal LET and a LET evaluated only at reload time), an IF block, LEFT JOIN, DROP, STORE, and comments that contain semicolons. |
| `reload.log` | The matching Qlik Sense reload log. Variables are expanded and line numbers match the script. |
| `Orders.qvd.header.xml` | The XML header of the stored `Orders.qvd`. Tests append binary bytes to it to build a QVD file. |

Expected values (the tests assert these):

- 16 statements, all classified, 0 `other`.
- `vReloadDate` cannot be evaluated from the script; it is recovered from the reload log as `2026-10-01`.
- Lines fetched: Customers 3,120 · RegionMap 2 · Orders 48,213 · the LEFT JOIN 3,120 (no label).
- `Orders.qvd`: 47,902 records, 8 fields, created by `Sales Overview.qvf`.
