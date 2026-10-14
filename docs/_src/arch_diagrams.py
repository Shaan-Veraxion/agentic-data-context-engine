"""Diagrams for the starting-stage architecture & workflow PDF (build_architecture_workflow.py).

Every function returns a reportlab Drawing sized to the content width.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_proposals import (  # noqa: E402
    NAVY, INK, MUTED, RULE, AGENT, AGENT_LINE, KNOW, KNOW_LINE, HITL, HITL_LINE, IFACE, IFACE_LINE, CONTENT_W,
)
from reportlab.lib import colors  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon, Group, PolyLine  # noqa: E402

W = CONTENT_W
ZONE = colors.HexColor("#F7F8FA")
OUTC = colors.HexColor("#E6ECF7"); OUTC_LINE = NAVY
WHITE = colors.white


# ------------------------------------------------------------------ primitives
def _fit(text, font, size, maxw):
    while size > 4.6 and pdfmetrics.stringWidth(text, font, size) > maxw:
        size -= 0.2
    return size


def text(d, x, y, s, size=6.6, font="Arial", color=MUTED, anchor="start", maxw=None):
    if maxw:
        size = _fit(s, font, size, maxw)
    d.add(String(x, y, s, fontName=font, fontSize=size, fillColor=color, textAnchor=anchor))


def vtext(d, x, y, s, size=6.4, font="Arial-Italic", color=MUTED):
    """Text rotated 90 degrees counter-clockwise, starting at (x, y)."""
    g = Group(String(0, 0, s, fontName=font, fontSize=size, fillColor=color))
    g.transform = (0, 1, -1, 0, x, y)
    d.add(g)


def rect(d, x, y, w, h, fill, line, dashed=False, r=4, sw=0.9):
    rr = Rect(x, y, w, h, rx=r, ry=r, fillColor=fill, strokeColor=line, strokeWidth=sw)
    if dashed:
        rr.strokeDashArray = [3, 2]
    d.add(rr)


def mbox(d, x, y, w, h, title, lines=(), fill=AGENT, line=AGENT_LINE, ts=7.8, ls=6.3, dashed=False):
    """Box with a centred bold title and centred muted lines, vertically centred as a block."""
    rect(d, x, y, w, h, fill, line, dashed)
    step = ls + 2.3
    block = ts + len(lines) * step
    base = y + h / 2 + block / 2 - ts * 0.82
    d.add(String(x + w / 2, base, title, fontName="Arial-Bold", fontSize=_fit(title, "Arial-Bold", ts, w - 6),
                 fillColor=INK, textAnchor="middle"))
    for i, ln in enumerate(lines):
        d.add(String(x + w / 2, base - (i + 1) * step, ln, fontName="Arial", fontSize=_fit(ln, "Arial", ls, w - 6),
                     fillColor=MUTED, textAnchor="middle"))


def listbox(d, x, y, w, h, title, lines, fill=AGENT, line=AGENT_LINE, ts=7.8, ls=6.4):
    """Box with a centred title on top and left-aligned list lines below it."""
    rect(d, x, y, w, h, fill, line)
    d.add(String(x + w / 2, y + h - 12, title, fontName="Arial-Bold", fontSize=_fit(title, "Arial-Bold", ts, w - 6),
                 fillColor=INK, textAnchor="middle"))
    d.add(Line(x + 6, y + h - 17, x + w - 6, y + h - 17, strokeColor=line, strokeWidth=0.4))
    for i, ln in enumerate(lines):
        d.add(String(x + 6, y + h - 27 - i * (ls + 3.4), ln, fontName="Arial",
                     fontSize=_fit(ln, "Arial", ls, w - 12), fillColor=INK))


def zone(d, x, y, w, h, label, lx=6):
    rect(d, x, y, w, h, ZONE, RULE, r=6, sw=0.6)
    d.add(String(x + lx, y + 3.5, label.upper(), fontName="Arial-Bold", fontSize=6.2, fillColor=NAVY))


def head(d, x2, y2, a, color, s=4.2):
    p1 = (x2 - s * math.cos(a - 0.45), y2 - s * math.sin(a - 0.45))
    p2 = (x2 - s * math.cos(a + 0.45), y2 - s * math.sin(a + 0.45))
    d.add(Polygon([x2, y2, p1[0], p1[1], p2[0], p2[1]], fillColor=color, strokeColor=color, strokeWidth=0.5))


def arr(d, pts, label=None, color=MUTED, dashed=False, both=False, lpos=0.5, ldx=0, ldy=0, lsize=5.9,
        lanchor="middle", sw=0.85, lseg=None):
    """Poly-line arrow through pts [(x,y),...]; arrow head at the end (and start if both)."""
    flat = [c for p in pts for c in p]
    pl = PolyLine(flat, strokeColor=color, strokeWidth=sw)
    if dashed:
        pl.strokeDashArray = [2.5, 2]
    d.add(pl)
    (xa, ya), (xb, yb) = pts[-2], pts[-1]
    head(d, xb, yb, math.atan2(yb - ya, xb - xa), color)
    if both:
        (xa, ya), (xb, yb) = pts[1], pts[0]
        head(d, xb, yb, math.atan2(yb - ya, xb - xa), color)
    if label:
        # label at fraction lpos along the first segment by default (or the middle segment for 3+ points)
        seg = lseg if lseg is not None else (len(pts) // 2 - 1 if len(pts) > 2 else 0)
        (x1, y1), (x2, y2) = pts[seg], pts[seg + 1]
        mx, my = x1 + (x2 - x1) * lpos + ldx, y1 + (y2 - y1) * lpos + ldy
        tw = pdfmetrics.stringWidth(label, "Arial-Italic", lsize)
        bx = mx - tw / 2 if lanchor == "middle" else (mx if lanchor == "start" else mx - tw)
        d.add(Rect(bx - 1.5, my - 2, tw + 3, lsize + 2.2, fillColor=WHITE, strokeColor=None))
        d.add(String(mx, my, label, fontName="Arial-Italic", fontSize=lsize, fillColor=MUTED, textAnchor=lanchor))


def legend(d, y, items=None, x=4):
    items = items or [("Agents / workers", AGENT, AGENT_LINE, False), ("Human in the loop", HITL, HITL_LINE, False),
                      ("Knowledge + storage", KNOW, KNOW_LINE, False), ("Interfaces / external", IFACE, IFACE_LINE, False),
                      ("Later stage", WHITE, MUTED, True)]
    for label, fill, line, dashed in items:
        rect(d, x, y, 9, 9, fill, line, dashed, r=2, sw=0.8)
        d.add(String(x + 13, y + 1.5, label, fontName="Arial", fontSize=7.2, fillColor=INK))
        x += 13 + pdfmetrics.stringWidth(label, "Arial", 7.2) + 15


# ------------------------------------------------------------------ Figure: context
def context_diagram():
    d = Drawing(W, 262)
    cw, ch = 132, 46
    rx = W - cw
    mbox(d, 0, 186, cw, ch, "Qlik artifacts", ["load scripts · -prj folders", "reload logs · QVDs · exports"], IFACE, IFACE_LINE)
    mbox(d, 0, 112, cw, ch, "Dremio (read-only)", ["catalog · view SQL · wikis", "data via bounded pushdown"], IFACE, IFACE_LINE)
    mbox(d, 0, 38, cw, ch, "Qlik servers", ["Engine API · live checks (V6)"], WHITE, MUTED, dashed=True)
    mbox(d, rx, 186, cw, ch, "Reviewers and owners", ["report owners · data owners", "answer · approve · correct"], HITL, HITL_LINE)
    mbox(d, rx, 112, cw, ch, "Google Cloud Vertex AI", ["Claude models via", "model-gateway only"], IFACE, IFACE_LINE)
    mbox(d, rx, 38, cw, ch, "Designed data model", ["star schema · normalized ·", "curated Dremio views"], WHITE, MUTED, dashed=True)
    cx, cy, cwid, chei = 168, 92, W - 336, 112
    rect(d, cx, cy, cwid, chei, KNOW, KNOW_LINE, r=7, sw=1.2)
    text(d, cx + cwid / 2, cy + chei - 18, "ADCE starting stage", 10, "Arial-Bold", NAVY, "middle")
    for i, s in enumerate(["Qlik-to-SQL reconstruction", "17 agents + orchestrator", "knowledge graph with evidence",
                           "validation ladder V0 – V5", "human review workbench"]):
        text(d, cx + cwid / 2, cy + chei - 36 - i * 13, s, 7.6, "Arial", INK, "middle")
    mbox(d, cx, 6, cwid, 42, "Migration pack per application", ["SQL · lineage · validation · review record"], OUTC, OUTC_LINE)
    arr(d, [(cw, 209), (cx, 180)], "scripts and files")
    arr(d, [(cw, 135), (cx, 140)], "metadata · SQL runs", both=True)
    arr(d, [(cw, 61), (cx, 104)], "later", dashed=True)
    arr(d, [(rx, 209), (cx + cwid, 180)], "decisions", both=True)
    arr(d, [(rx, 135), (cx + cwid, 140)], "AI calls", both=True)
    arr(d, [(cx + cwid, 104), (rx, 61)], "later", dashed=True)
    arr(d, [(W / 2, cy), (W / 2, 48)], "export")
    text(d, W / 2, 252, "Read-only towards Dremio and Qlik · no row-level data sent to AI models · every result labelled with its evidence level",
         6.8, "Arial-Italic", MUTED, "middle")
    return d


# ------------------------------------------------------------------ Figure: deep architecture (full page)
def deep_architecture():
    H = 646
    d = Drawing(W, H)
    full = d
    d = Drawing(W, H)  # upper section, drawn at nominal coordinates and shifted down 30pt below
    # actors / inputs / outputs
    mbox(d, 0, 630, 150, 38, "Reviewers and owners", ["report owners · data owners · stewards"], HITL, HITL_LINE)
    mbox(d, 165, 630, 165, 38, "Qlik artifacts (input)", ["scripts · -prj · reload logs · QVDs · exports"], IFACE, IFACE_LINE)
    mbox(d, 345, 630, 136, 38, "Migration pack (output)", ["SQL · lineage · validation · record"], OUTC, OUTC_LINE)

    # interfaces
    zone(d, 0, 552, W, 62, "Interfaces")
    bw = 146
    mbox(d, 8, 566, bw, 40, "Review workbench", ["Next.js · React Flow", "overview · queue · lineage"], IFACE, IFACE_LINE)
    mbox(d, 168, 566, bw, 40, "API gateway", ["FastAPI · REST + SSE", "uploads · exports · AuthN/Z"], IFACE, IFACE_LINE)
    mbox(d, 327, 566, bw, 40, "Keycloak", ["OIDC single sign-on · roles", "service accounts per worker"], IFACE, IFACE_LINE)
    arr(d, [(75, 630), (81, 606)], "HTTPS")
    arr(d, [(247, 630), (241, 606)], "upload")
    arr(d, [(300, 606), (390, 630)], "export", lpos=0.55)
    arr(d, [(154, 586), (168, 586)])
    arr(d, [(314, 586), (327, 586)], both=True)

    # control plane
    zone(d, 0, 458, 386, 84, "Control plane")
    cw = 118
    listbox(d, 8, 470, cw, 64, "orchestrator", ["LangGraph 1.x supervisor", "qlik_pipeline subgraph",
                                                "Postgres checkpointer", "fan-out per table (Send)"], AGENT, AGENT_LINE, ls=6.1)
    listbox(d, 134, 470, cw, 64, "hitl-service", ["Ask · Queue · Gate", "Seed · Correct", "rank: impact × uncertainty",
                                                  "decision → fact + event"], HITL, HITL_LINE, ls=6.1)
    listbox(d, 260, 470, cw, 64, "model-gateway", ["route by task · structured out", "prompt cache · memoization",
                                                   "record / replay · budgets", "privacy guard (no rows)"], IFACE, IFACE_LINE, ls=6.1)
    mbox(d, 395, 470, 86, 64, "Vertex AI", ["Google Cloud project", "Claude Opus 5.5", "Sonnet 5.5 · Haiku 4.5",
                                            "(Gemini optional)"], IFACE, IFACE_LINE, ls=5.9)
    arr(d, [(378, 502), (395, 502)], both=True)
    arr(d, [(205, 566), (80, 534)], "start run", lpos=0.45)
    arr(d, [(241, 566), (215, 534)], "decisions", lpos=0.45)
    upper = Group(*d.contents)
    upper.transform = (1, 0, 0, 1, 0, -30)
    d = full
    d.add(upper)

    # agent workers
    zone(d, 0, 294, W, 122, "Agent workers", lx=205)
    ww = (W - 16 - 3 * 8) / 4
    xs = [8 + i * (ww + 8) for i in range(4)]
    workers = [
        ("agents-ingestion", ["1   Intake router  · D", "2   Schema parser  · D", "4   Entity linker (lite)  · H",
                              "5   Scope registrar  · D"]),
        ("agents-bi-adapters", ["27  BI extractor  · D", "28  Script translator  · H", "29  Expression translator  · H",
                                "reads Qlik files only"]),
        ("agents-reversesql", ["21  Report parser  · H", "22  Metric mapper  · H", "23  Join planner  · H",
                               "24  Synthesizer  · H", "25  Query checker  · H", "26  Reconciler  · H"]),
        ("support agents", ["9   Key discoverer (lite)  · D", "19  Lineage (lite)  · D", "20  Publisher (lite)  · D",
                            "30  Equivalence (lite)  · H"]),
    ]
    for x, (n, ls) in zip(xs, workers):
        listbox(d, x, 306, ww, 94, n, ls, AGENT, AGENT_LINE, ls=6.2)
    # dispatch bus
    d.add(Line(67, 440, 67, 408, strokeColor=AGENT_LINE, strokeWidth=0.9))
    d.add(Line(xs[0] + ww / 2, 408, xs[3] + ww / 2, 408, strokeColor=AGENT_LINE, strokeWidth=0.9))
    for x in xs:
        arr(d, [(x + ww / 2, 408), (x + ww / 2, 400)], color=AGENT_LINE)
    text(d, 63, 419, "dispatch jobs", 5.9, "Arial-Italic", anchor="end")
    arr(d, [(xs[2] + ww - 14, 400), (319, 440)], "AI calls", color=AGENT_LINE, dashed=True, lpos=0.62)

    # knowledge service
    zone(d, 0, 212, W, 72, "Knowledge access  ·  the only writer of knowledge", lx=262)
    rect(d, 8, 224, W - 16, 52, KNOW, KNOW_LINE)
    text(d, W / 2, 265, "kg-mcp  ·  FastMCP 3.x over streamable HTTP  ·  implements kg_port", 7.8, "Arial-Bold", INK, "middle")
    inner = ["typed tools", "policy engine", "validator", "trust gate → HITL", "context packs", "outbox projector"]
    iw = (W - 16 - 12 - 5 * 5) / 6
    for i, s in enumerate(inner):
        mbox(d, 14 + i * (iw + 5), 230, iw, 24, s, [], WHITE, KNOW_LINE, ts=6.6)
    for x in xs:
        arr(d, [(x + ww / 2, 306), (x + ww / 2, 276)], "MCP" if x == xs[0] else None, color=KNOW_LINE)
    gx = xs[1] - 4  # gap between the first two worker boxes
    arr(d, [(gx, 276), (gx, 426), (165, 426), (165, 440)], None, color=HITL_LINE, dashed=True)
    text(d, 169, 429, "gate items", 5.9, "Arial-Italic")

    # storage
    zone(d, 0, 132, W, 70, "Storage")
    sw = (W - 16 - 4 * 6) / 5
    stores = [("Postgres 16", ["fact_log (source of truth)", "outbox · HITL items", "graph checkpoints"], KNOW, KNOW_LINE),
              ("Neo4j 5.26 LTS", ["graph projection", "derived · rebuildable", "read-only Cypher"], KNOW, KNOW_LINE),
              ("MinIO (GCS in cloud)", ["inputs · BI IR · SQL", "migration packs", "Parquet extracts"], KNOW, KNOW_LINE),
              ("DuckDB + Polars", ["in-process compute", "files and extracts", "no server"], KNOW, KNOW_LINE),
              ("Langfuse v3", ["traces · cost · evals", "ClickHouse · Redis · S3", "via OpenTelemetry"], KNOW, KNOW_LINE)]
    for i, (n, ls, f, l) in enumerate(stores):
        mbox(d, 8 + i * (sw + 6), 144, sw, 50, n, ls, f, l, ts=7.2, ls=6.0)
    arr(d, [(8 + sw / 2, 224), (8 + sw / 2, 194)], "SQL (one tx)", color=KNOW_LINE)
    arr(d, [(14 + sw * 1.5, 224), (14 + sw * 1.5, 194)], "Bolt", color=KNOW_LINE)
    arr(d, [(20 + sw * 2.5, 224), (20 + sw * 2.5, 194)], "S3 API", color=KNOW_LINE)

    # external sources
    zone(d, 0, 54, W, 68, "External systems")
    mbox(d, 8, 64, 190, 50, "Qlik servers (later)", ["Qlik Engine API over WebSocket", "QlikView / Qlik Sense",
                                                     "live validation (V6)"], WHITE, MUTED, dashed=True, ls=6.0)
    mbox(d, 206, 64, 267, 50, "Dremio (read-only service account)",
         ["Arrow Flight SQL via ADBC · REST catalog API v3", "view SQL · wikis · tags · sys.jobs_recent",
          "Iceberg time travel: AT TIMESTAMP / AT SNAPSHOT"], IFACE, IFACE_LINE, ls=6.0)
    # data_port channel on the right edge
    d.add(Line(xs[3] + ww, 330, 478, 330, strokeColor=IFACE_LINE, strokeWidth=0.9))
    arr(d, [(478, 330), (478, 89), (473, 89)], color=IFACE_LINE)
    text(d, 474, 125, "data_port: Flight SQL · REST", 5.8, "Arial-Italic", anchor="end")

    # shared library
    mbox(d, 0, 20, W, 26, "adce-core shared library (used by every service)",
         ["AgentSpec · BI IR · ExprAST · fact and event schemas · kg_port · data_port · gateway client"],
         IFACE, IFACE_LINE, ts=7.2, ls=6.0)
    legend(d, 2)
    return d


# ------------------------------------------------------------------ Figure: workflow swimlane
LANES = [("Inputs and Dremio", 0, 88), ("Agents", 94, 132), ("Knowledge (kg-mcp)", 232, 98),
         ("Human review", 336, 76), ("Outputs", 418, W - 418)]


def swimlane():
    rows = [
        ("1  Intake", ["1 Intake router", "classify · store · scope"], ("Qlik files", ["scripts · -prj · logs", "QVDs · exports"]),
         ("BIApp + inputs", ["registered"]), ("Gate", ["confirm app scope"]), None),
        ("2  Catalog crawl", ["5 Scope registrar · 2 Schema parser", "spaces · datasets · view SQL"],
         ("Dremio catalog", ["REST API v3"]), ("Dataset · Column", ["view lineage: documented"]), None, None),
        ("3  Extract", ["27 BI extractor", "split · parse · resolve variables"], ("Reload logs", ["variable values"]),
         ("BI IR facts", ["tables · fields · QVD chains"]), ("Ask", ["unresolved variable"]), None),
        ("4  Resolve sources", ["4-lite · 5", "FROM → Dremio dataset / producer"], ("Connection map", ["Qlik lib → Dremio"]),
         ("scope_link", ["inferred → confirmed"]), ("Queue", ["uncertain mapping"]), None),
        ("5  Grain", ["9-lite Key discoverer", "uniqueness of candidate keys"], ("Dremio", ["COUNT / DISTINCT pushdown"]),
         ("primary_key", ["validated"]), None, None),
        ("6  Translate", ["28 Script · 29 Expression", "rules first · AI for the long tail"],
         ("model-gateway", ["Sonnet / Opus on Vertex"]), ("bi_transform", ["bi_metric_def"]),
         ("Queue", ["untranslated statement"]), None),
        ("7  Compose", ["22 Metric mapper · 23 Join planner", "24 Synthesizer: CTE chains"], None,
         ("context pack", ["keys · joins · grain"]), None, None),
        ("8  Check", ["25 Query checker", "dialect · grain · fan-out · set rules"], None,
         ("sql.checked", ["rule results kept"]), None, None),
        ("9  Reconcile", ["26 Reconciler · 21 Report parser", "execute · compare · diagnose"],
         ("Dremio + evidence", ["run · logs · QVDs · exports"]), ("report_sql", ["level V0 – V5"]),
         ("Gate", ["approve final SQL"]), ("Validation", ["report"])),
        ("10  Review", ["hitl-service", "decisions become facts"], None, ("fact.confirmed", ["re-runs affected steps"]),
         ("Workbench", ["Ask · Queue · Gate"]), None),
        ("11  Lineage + export", ["19-lite Lineage · 20-lite Publisher", "assemble · write · version"], None,
         ("lineage graph", ["OpenLineage JSON"]), None, ("Migration", ["pack"])),
    ]
    rh = 50
    top = 26 + len(rows) * rh
    H = top + 24
    d = Drawing(W, H)
    for i, (name, x, w) in enumerate(LANES):
        rect(d, x, 18, w, top - 18 + 22, ZONE if i % 2 == 0 else WHITE, RULE, r=3, sw=0.5)
        text(d, x + w / 2, top + 7, name, 7.2, "Arial-Bold", NAVY, "middle", maxw=w - 4)
    for r, (title, al, inp, kg, hu, out) in enumerate(rows):
        y = top - (r + 1) * rh + 7
        bh = rh - 14
        ax, aw = LANES[1][1] + 4, LANES[1][2] - 8
        mbox(d, ax, y, aw, bh, title, al, AGENT, AGENT_LINE, ts=7.4, ls=5.9)
        if r < len(rows) - 1:
            arr(d, [(ax + aw / 2, y), (ax + aw / 2, y - 14)], color=AGENT_LINE)
        mid = y + bh / 2
        if inp:
            x, w = LANES[0][1] + 4, LANES[0][2] - 8
            mbox(d, x, y + 3, w, bh - 6, inp[0], inp[1], IFACE, IFACE_LINE, ts=6.9, ls=5.6)
            arr(d, [(x + w, mid), (ax, mid)])
        if kg:
            x, w = LANES[2][1] + 4, LANES[2][2] - 8
            mbox(d, x, y + 3, w, bh - 6, kg[0], kg[1], KNOW, KNOW_LINE, ts=6.9, ls=5.6)
            if r == 6:
                arr(d, [(x, mid), (ax + aw, mid)], color=KNOW_LINE)
            else:
                arr(d, [(ax + aw, mid), (x, mid)], color=KNOW_LINE)
        if hu:
            x, w = LANES[3][1] + 4, LANES[3][2] - 8
            mbox(d, x, y + 3, w, bh - 6, hu[0], hu[1], HITL, HITL_LINE, ts=6.9, ls=5.6)
            kx = LANES[2][1] + LANES[2][2] - 4
            if r == 9:
                arr(d, [(x, mid), (kx, mid)], color=HITL_LINE)
            else:
                arr(d, [(kx, mid), (x, mid)], color=HITL_LINE, both=True)
        if out:
            x, w = LANES[4][1] + 4, LANES[4][2] - 8
            mbox(d, x, y + 3, w, bh - 6, out[0], out[1], OUTC, OUTC_LINE, ts=6.9, ls=5.6)
            kx2 = LANES[2][1] + LANES[2][2] - 4
            arr(d, [(kx2, y + 8), (kx2 + 3, y + 8), (kx2 + 3, y), (x + w / 2, y), (x + w / 2, y + 3)], color=OUTC_LINE)
    # repair loop: check (row 8) back to compose (row 7)
    y7 = top - 7 * rh + 7 + (rh - 14) / 2
    y8 = top - 8 * rh + 7 + (rh - 14) / 2
    xr = LANES[1][1] + LANES[1][2] - 4
    arr(d, [(xr, y8 + 4), (xr + 3, y8 + 4), (xr + 3, y7 - 4), (xr, y7 - 4)], color=AGENT_LINE, dashed=True)
    # re-run loop: review (row 10) back to extract (row 3) on the lane boundary
    y10 = top - 10 * rh + 7 + (rh - 14) / 2
    y3 = top - 3 * rh + 7 + (rh - 14) / 2
    xl = LANES[1][1] + 4
    arr(d, [(xl, y10), (xl - 3, y10), (xl - 3, y3), (xl, y3)], color=HITL_LINE, dashed=True)
    vtext(d, xl - 5.5, y10 + 30, "re-run only the affected steps", 5.6)
    legend(d, 2, [("Agent step", AGENT, AGENT_LINE, False), ("Input / Dremio", IFACE, IFACE_LINE, False),
                  ("Knowledge", KNOW, KNOW_LINE, False), ("Human review", HITL, HITL_LINE, False),
                  ("Output", OUTC, OUTC_LINE, False)])
    return d


# ------------------------------------------------------------------ Figure: one table, end to end
def table_flow():
    d = Drawing(W, 168)
    g = 14
    bw = (W - 4 * g) / 5
    r1 = [("Script text", [".qvs · -prj script", "include files inlined"], IFACE, IFACE_LINE),
          ("Statements", ["splitter · source spans", "comments, strings safe"], AGENT, AGENT_LINE),
          ("Parse tree", ["Lark grammar", "unknown → kind: other"], AGENT, AGENT_LINE),
          ("BI IR table", ["fields · sources", "transforms · keys"], KNOW, KNOW_LINE),
          ("Resolved sources", ["Dremio dataset or", "QVD producer app"], KNOW, KNOW_LINE)]
    r2 = [("Fact + level", ["report_sql · V0 – V5", "evidence attached"], KNOW, KNOW_LINE),
          ("Compared", ["counts · totals · figures", "time-aligned"], AGENT, AGENT_LINE),
          ("Run in Dremio", ["ADBC Flight SQL", "bounded · labelled"], IFACE, IFACE_LINE),
          ("Checked SQL", ["dremio dialect", "grain · fan-out rules"], AGENT, AGENT_LINE),
          ("SQL AST", ["sqlglot · one CTE", "per statement"], AGENT, AGENT_LINE)]
    for i, (t, ls, f, l) in enumerate(r1):
        x = i * (bw + g)
        mbox(d, x, 104, bw, 46, t, ls, f, l)
        text(d, x + 4, 153, f"{i + 1}", 7, "Arial-Bold", NAVY)
        if i < 4:
            arr(d, [(x + bw, 127), (x + bw + g, 127)])
    for i, (t, ls, f, l) in enumerate(r2):
        x = i * (bw + g)
        mbox(d, x, 30, bw, 46, t, ls, f, l)
        text(d, x + 4, 79, f"{10 - i}", 7, "Arial-Bold", NAVY)
        if i < 4:
            arr(d, [(x + bw + g, 53), (x + bw, 53)])
    arr(d, [(W - bw / 2, 104), (W - bw / 2, 76)], "ExprAST for measures", lpos=0.5, ldx=-4, lanchor="end")
    text(d, 0, 10, "Numbers show the order. Every box is stored: inputs and BI IR in MinIO, results as facts in the knowledge graph.",
         7.0, "Arial-Italic")
    return d


# ------------------------------------------------------------------ Figure: orchestration graph
def orchestration_graph():
    d = Drawing(W, 280)
    nw, nh = 92, 34
    cols = [0, 129, 258, W - nw]
    rows = [236, 160, 84, 12]

    def node(c, r, t, ls, f=AGENT, l=AGENT_LINE, dashed=False):
        mbox(d, cols[c], rows[r], nw, nh, t, ls, f, l, ts=7.4, ls=5.9, dashed=dashed)
        return cols[c], rows[r]

    node(0, 0, "intake", ["1 · classify, scope"])
    node(1, 0, "catalog", ["5 · 2 · crawl Dremio"])
    node(2, 0, "extract", ["27 · BI IR"])
    node(3, 0, "resolve", ["4-lite · 5 · sources"])
    node(3, 1, "grain", ["9-lite · keys"])
    node(2, 1, "translate", ["28 · 29 · per table (Send)"])
    node(1, 1, "compose", ["22 · 23 · 24"])
    node(0, 1, "check", ["25 · rules + repair"])
    node(0, 2, "reconcile", ["26 · 21 · execute, compare"])
    node(1, 2, "diagnose", ["26 · classify mismatch"], HITL, HITL_LINE)
    node(2, 2, "publish", ["19-lite · 20-lite"])
    mbox(d, cols[3], rows[2], nw, nh, "END", ["pack.published"], OUTC, OUTC_LINE, ts=7.4, ls=5.9)
    yc = lambda r: rows[r] + nh / 2  # noqa: E731
    for c in range(3):
        arr(d, [(cols[c] + nw, yc(0)), (cols[c + 1], yc(0))], color=AGENT_LINE)
    arr(d, [(cols[3] + nw / 2, rows[0]), (cols[3] + nw / 2, rows[1] + nh)], color=AGENT_LINE)
    arr(d, [(cols[3], yc(1)), (cols[2] + nw, yc(1))], color=AGENT_LINE)
    arr(d, [(cols[2], yc(1)), (cols[1] + nw, yc(1))], color=AGENT_LINE)
    arr(d, [(cols[1], yc(1) + 6), (cols[0] + nw, yc(1) + 6)], "SQL", color=AGENT_LINE)
    arr(d, [(cols[0] + nw, yc(1) - 6), (cols[1], yc(1) - 6)], "repair ≤ 2", color=HITL_LINE, dashed=True)
    arr(d, [(cols[0] + nw / 2, rows[1]), (cols[0] + nw / 2, rows[2] + nh)], "pass", color=AGENT_LINE, ldx=-12)
    arr(d, [(cols[0] + nw, yc(2) + 6), (cols[1], yc(2) + 6)], "mismatch", color=HITL_LINE, dashed=True)
    arr(d, [(cols[1] + nw / 2, rows[2] + nh), (cols[1] + nw / 2, rows[1])], "re-plan", color=HITL_LINE, dashed=True,
        ldx=16)
    arr(d, [(cols[1] + nw, rows[2] + nh - 4), (cols[2] + 14, rows[1])], "re-translate", color=HITL_LINE, dashed=True,
        lpos=0.55)
    arr(d, [(cols[0] + nw, yc(2) - 8), (cols[0] + nw + 8, yc(2) - 8), (cols[0] + nw + 8, rows[2] - 10),
            (cols[2] + nw / 2, rows[2] - 10), (cols[2] + nw / 2, rows[2])], "level reached", color=AGENT_LINE, lseg=2)
    arr(d, [(cols[2] + nw, yc(2)), (cols[3], yc(2))], color=AGENT_LINE)
    # HITL notes row
    rect(d, 0, rows[3], 252, 46, HITL, HITL_LINE)
    text(d, 6, rows[3] + 34, "Human review inside the graph", 7.4, "Arial-Bold", INK)
    text(d, 6, rows[3] + 23, "Ask: interrupt() pauses this table only; resumes on the answer.", 6.2, color=INK)
    text(d, 6, rows[3] + 13, "Queue and Gate: stored as data; the run continues with", 6.2, color=INK)
    text(d, 6, rows[3] + 4, "facts marked proposed (untrusted downstream).", 6.2, color=INK)
    rect(d, 262, rows[3], W - 262, 46, KNOW, KNOW_LINE)
    text(d, 268, rows[3] + 34, "Re-entry and durability", 7.4, "Arial-Bold", INK)
    text(d, 268, rows[3] + 23, "fact.confirmed / dataset.changed → re-enter at the affected node.", 6.2, color=INK)
    text(d, 268, rows[3] + 13, "Checkpoint after every node (Postgres); jobs idempotent by", 6.2, color=INK)
    text(d, 268, rows[3] + 4, "stable fact IDs, so crashes resume and retries are safe.", 6.2, color=INK)
    return d


# ------------------------------------------------------------------ Figure: knowledge write path (sequence)
def write_sequence():
    life = ["Agent", "kg-mcp", "Validator + policy", "Postgres", "Projector", "Neo4j", "hitl-service"]
    msgs = [
        (0, 1, "propose_fact(fact, evidence)", False),
        (1, 2, "schema · scope · trust ceiling", False),
        (2, 1, "ok / reject + reason", True),
        (1, 3, "INSERT fact_log + outbox (one tx)", False),
        (1, 0, "fact_id · status = proposed", True),
        (3, 4, "outbox row (NOTIFY / poll)", False),
        (4, 5, "MERGE nodes · current-view edges", False),
        (1, 6, "gate item if confidence × impact needs review", False),
        (6, 1, "decision: confirm / reject / correct", False),
        (1, 3, "append decision fact (supersedes)", False),
        (4, 5, "update current view", False),
        (3, 0, "fact.confirmed → orchestrator re-runs affected steps", True),
    ]
    step = 22
    H = 40 + len(msgs) * step - 4
    d = Drawing(W, H)
    sp = W / len(life)
    xs = [sp * i + sp / 2 for i in range(len(life))]
    fills = [(AGENT, AGENT_LINE), (KNOW, KNOW_LINE), (KNOW, KNOW_LINE), (KNOW, KNOW_LINE), (KNOW, KNOW_LINE),
             (KNOW, KNOW_LINE), (HITL, HITL_LINE)]
    for x, n, (f, l) in zip(xs, life, fills):
        mbox(d, x - sp / 2 + 3, H - 24, sp - 6, 22, n, [], f, l, ts=7)
        ln = Line(x, H - 24, x, 6, strokeColor=RULE, strokeWidth=0.7)
        ln.strokeDashArray = [2, 2]
        d.add(ln)
    for i, (a, b, label, ret) in enumerate(msgs):
        y = H - 40 - i * step
        col = MUTED if not ret else NAVY
        arr(d, [(xs[a], y), (xs[b], y)], color=col, dashed=ret)
        lx = (xs[a] + xs[b]) / 2
        text(d, lx, y + 3, label, 6.0, "Arial", INK, "middle", maxw=max(abs(xs[b] - xs[a]) - 4, 120))
        text(d, 2, y - 2, f"{i + 1}", 6.4, "Arial-Bold", NAVY)
    return d


# ------------------------------------------------------------------ Figure: AI call path
def ai_call_path():
    d = Drawing(W, 176)
    g = 14
    bw = (W - 4 * g) / 5
    r1 = [("Agent request", ["route · inputs", "output schema"], AGENT, AGENT_LINE),
          ("Privacy guard", ["no row-shaped data", "allowed fields only"], IFACE, IFACE_LINE),
          ("Memo lookup", ["hash(inputs, model,", "prompt version)"], KNOW, KNOW_LINE),
          ("Budget check", ["per task · per run", "retry caps"], IFACE, IFACE_LINE),
          ("Prompt assembly", ["stable prefix cached", "examples from KG"], IFACE, IFACE_LINE)]
    r2 = [("Return to agent", ["typed result", "+ trace id"], AGENT, AGENT_LINE),
          ("Record + trace", ["replay store", "Langfuse: tokens, cost"], KNOW, KNOW_LINE),
          ("Schema validation", ["retry once, then", "Queue for review"], IFACE, IFACE_LINE),
          ("Refusal / error", ["client-side fallback", "backoff on 429 / 5xx"], IFACE, IFACE_LINE),
          ("Vertex AI", ["Claude Sonnet / Opus", "structured outputs"], IFACE, IFACE_LINE)]
    for i, (t, ls, f, l) in enumerate(r1):
        x = i * (bw + g)
        mbox(d, x, 112, bw, 44, t, ls, f, l)
        if i < 4:
            arr(d, [(x + bw, 134), (x + bw + g, 134)], "miss" if i == 2 else None)
    for i, (t, ls, f, l) in enumerate(r2):
        x = i * (bw + g)
        mbox(d, x, 36, bw, 44, t, ls, f, l)
        if i < 4:
            arr(d, [(x + bw + g, 58), (x + bw, 58)])
    arr(d, [(W - bw / 2, 112), (W - bw / 2, 80)])
    hx = 2 * (bw + g) + bw / 2
    arr(d, [(hx, 112), (hx, 96), (bw / 2, 96), (bw / 2, 80)], "memo hit: same output, no tokens", color=KNOW_LINE,
        dashed=True)
    text(d, 0, 18, "Replay mode (tests, CI, demos) answers every call from the replay store: no network, no token spend.",
         7.0, "Arial-Italic")
    text(d, 0, 6, "Batch API, server-side fallbacks and task budgets are not on Vertex AI, so the gateway provides those behaviours.",
         7.0, "Arial-Italic")
    return d


# ------------------------------------------------------------------ Figure: review lifecycle
def review_lifecycle():
    d = Drawing(W, 214)
    bw, bh = 96, 36
    c = [0, 128, 256, W - bw]

    def st(ci, y, t, ls, f, l):
        mbox(d, c[ci], y, bw, bh, t, ls, f, l, ts=7.4, ls=5.9)

    st(0, 96, "proposed", ["agent output + evidence"], KNOW, KNOW_LINE)
    st(1, 170, "accepted", ["auto: rule or evidence", "trust inferred / validated"], KNOW, KNOW_LINE)
    st(1, 96, "in review", ["Queue or Gate item", "ranked by impact"], HITL, HITL_LINE)
    st(1, 22, "asked", ["Ask: table paused", "agent waits for answer"], HITL, HITL_LINE)
    st(2, 170, "confirmed", ["trust: confirmed", "reused as an example"], KNOW, KNOW_LINE)
    st(2, 96, "corrected", ["reviewer edit →", "new confirmed fact"], HITL, HITL_LINE)
    st(2, 22, "answered", ["seed fact confirmed", "table resumes"], KNOW, KNOW_LINE)
    st(3, 170, "superseded", ["kept in history", "never deleted"], WHITE, MUTED)
    st(3, 96, "rejected", ["kept as a negative", "example; not re-proposed"], WHITE, MUTED)
    st(3, 22, "conflict flagged", ["new evidence disagrees", "→ back into review"], HITL, HITL_LINE)
    y = lambda v: v + bh / 2  # noqa: E731
    arr(d, [(bw, y(96) + 8), (c[1], y(170))], "low impact", lpos=0.5)
    arr(d, [(bw, y(96)), (c[1], y(96))])
    arr(d, [(bw, y(96) - 8), (c[1], y(22))], "blocking", lpos=0.5)
    arr(d, [(c[1] + bw, y(96) + 8), (c[2], y(170))], "confirm")
    arr(d, [(c[1] + bw, y(96)), (c[2], y(96))], "correct")
    arr(d, [(c[1] + bw, y(22)), (c[2], y(22))])
    arr(d, [(c[2] + bw, y(96)), (c[3], y(170) - 4)], "old version", lpos=0.4)
    arr(d, [(c[1] + bw, y(96) - 12), (c[1] + bw + 6, y(96) - 12), (c[1] + bw + 6, 82), (c[3] + bw / 2, 82),
            (c[3] + bw / 2, 96)], "reject", lpos=0.5)
    arr(d, [(c[2] + bw, y(170)), (c[3], y(170))], "replaced later")
    arr(d, [(c[3] + bw / 2, 22 + bh), (c[3] + bw / 2, 60), (c[1] + bw / 2, 60), (c[1] + bw / 2, 96)], None,
        color=HITL_LINE, dashed=True)
    arr(d, [(c[1] + bw / 2, 170), (c[1] + bw / 2, 132)], "conflict", color=HITL_LINE, dashed=True, ldx=18)
    text(d, 0, 4, "Every state change is an appended fact; nothing is overwritten. The fact log keeps both valid time and record time.",
         7.0, "Arial-Italic")
    return d


# ------------------------------------------------------------------ Figure: deployment
def deployment():
    d = Drawing(W, 300)
    cw = (W - 2 * 22) / 3
    xs = [0, cw + 22, 2 * (cw + 22)]
    cols = [
        ("Local (now)", "Docker Compose with profiles", [
            ("core", ["api-gateway · workbench · orchestrator", "hitl-service · model-gateway · kg-mcp", "all agent workers"]),
            ("stores", ["Postgres · Neo4j · MinIO", "Keycloak (dev realm)"]),
            ("dremio-local", ["Dremio OSS + sample data", "or the shared dev Dremio"]),
            ("observability", ["Langfuse · OpenTelemetry"]),
            ("models", ["Vertex dev project", "or record / replay (offline)"])]),
        ("Staging (Google Cloud)", "Same images · Helm chart", [
            ("GKE", ["every service as a", "Deployment, autoscaled workers"]),
            ("Cloud SQL", ["Postgres 16 + pgvector", "point-in-time recovery"]),
            ("Neo4j", ["StatefulSet or managed", "rebuildable from fact log"]),
            ("GCS · Secret Manager", ["artifacts and packs", "credentials, keys"]),
            ("Vertex AI · Dremio", ["staging project", "read-only service account"])]),
        ("Production / client", "Same images · same chart", [
            ("Client Kubernetes or GKE", ["installed by Helm", "values per environment"]),
            ("Client stores", ["their Postgres / bucket", "or bundled defaults"]),
            ("Client model endpoint", ["their Vertex project or", "approved provider"]),
            ("Client identity", ["Keycloak federated to", "their IdP (OIDC / SAML)"]),
            ("Air-gap option", ["offline images · SBOM", "signed releases · support bundle"])]),
    ]
    for x, (title, sub, boxes) in zip(xs, cols):
        rect(d, x, 22, cw, 270, ZONE, RULE, r=6, sw=0.6)
        text(d, x + cw / 2, 279, title, 8.4, "Arial-Bold", NAVY, "middle")
        text(d, x + cw / 2, 268, sub, 6.8, "Arial-Italic", MUTED, "middle")
        for i, (t, ls) in enumerate(boxes):
            f, l = (KNOW, KNOW_LINE) if t in ("stores", "Cloud SQL", "Neo4j", "GCS · Secret Manager", "Client stores", "observability") \
                else (IFACE, IFACE_LINE) if "model" in t.lower() or "Vertex" in t or "identity" in t \
                or "dremio" in t.lower() or "Air-gap" in t else (AGENT, AGENT_LINE)
            mbox(d, x + 6, 216 - i * 48, cw - 12, 42, t, ls, f, l, ts=7.4, ls=6.0)
    for i in range(2):
        arr(d, [(xs[i] + cw, 150), (xs[i + 1], 150)], color=NAVY, sw=1.2)
    text(d, W / 2, 6, "One build promoted unchanged: images signed, configuration only through Helm values and secrets.",
         7.0, "Arial-Italic", MUTED, "middle")
    return d
