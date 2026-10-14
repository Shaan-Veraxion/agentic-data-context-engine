"""Build docs/architecture-alt-qlik-first.md from the template plus verbatim sections of docs/architecture.md."""
import re, sys

tpl_path, main_path, out_path = sys.argv[1:4]
tpl = open(tpl_path, encoding="utf-8").read()
main = open(main_path, encoding="utf-8").read()


def body(start_heading, stop_patterns=(r"\n---\n", r"\n## ", r"\n### ")):
    i = main.index(start_heading)
    i = main.index("\n", i) + 1
    ends = []
    for p in stop_patterns:
        m = re.compile(p).search(main, i)
        if m:
            ends.append(m.start())
    j = min(ends) if ends else len(main)
    return main[i:j].strip("\n")


blocks = {
    "VERDICT": body("## 1. Verdict on the proposal", (r"\n---\n",)),
    "RESEARCH": body("## 2. Research check", (r"\n---\n",)),
    "SYSTEM": body("## 3. System architecture", (r"\n---\n",)),
    "CAT_INGEST": body("### 5.1 Ingestion group"),
    "CAT_PROFILING": body("### 5.2 Profiling group"),
    "CAT_COMPARISON": body("### 5.3 Comparison group"),
    "CAT_DEFINITION": body("### 5.4 Definition and lineage group"),
    "CAT_REVERSESQL": body("### 5.5 Reverse-SQL group"),
    "CAT_ORCH": body("### 5.6 Orchestrator"),
    "CAT_BI": body("### 5.7 BI source-adapter agents (CP-001)"),
    "CAT_CONTRACTS": body("### 5.8 Contracts and events added by CP-001"),
    "TECH": body("## 6. Tech stack", (r"\n---\n",)),
    "METRICS": body("### 7.3 Success metrics", (r"\n---\n", r"\n## ")),
    "REPO": body("## 8. Proposed repository layout", (r"\n---\n",)),
    "RISKS": body("## 9. Risks", (r"\n---\n",)),
    "NONGOALS": body("## 11. Non-goals", (r"\n---\n",)),
    "SOURCES": body("## Sources", (r"\Z",)),
}

# Shared ADRs: everything from ADR-1 up to section 5, headings demoted one level for section 4.3.
a = main.index("### ADR-1:")
b = main.index("\n---\n\n## 5.", a)
adrs = main[a:b].strip("\n")
adrs = re.sub(r"(?m)^### ADR-", "#### ADR-", adrs)
adrs = adrs.replace("full list in section 5.8", "full list in section 5.6")
blocks["ADRS"] = adrs

for k, v in blocks.items():
    assert v.strip(), k
    tpl = tpl.replace("{{" + k + "}}", v)
left = re.findall(r"\{\{[A-Z_]+\}\}", tpl)
assert not left, left
open(out_path, "w", encoding="utf-8").write(tpl)
print("ok", len(tpl.splitlines()), "lines")
