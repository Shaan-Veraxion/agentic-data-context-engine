"""Build the two shareable proposal PDFs into docs/proposals/.

    python docs/_src/build_proposals.py

Content is intentionally external-facing: no team sizes, staffing or internal team structure.
"""
import os
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak, KeepTogether, NextPageTemplate, ListFlowable,
                                ListItem)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "proposals")
os.makedirs(OUT, exist_ok=True)

FONTS = "C:/Windows/Fonts"
pdfmetrics.registerFont(TTFont("Arial", f"{FONTS}/arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", f"{FONTS}/arialbd.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Italic", f"{FONTS}/ariali.ttf"))
pdfmetrics.registerFont(TTFont("Arial-BoldItalic", f"{FONTS}/arialbi.ttf"))
pdfmetrics.registerFontFamily("Arial", normal="Arial", bold="Arial-Bold", italic="Arial-Italic",
                              boldItalic="Arial-BoldItalic")

NAVY = colors.HexColor("#1F3864")
INK = colors.HexColor("#1A1A1A")
MUTED = colors.HexColor("#5A6270")
RULE = colors.HexColor("#C9CFD8")
HEAD_BG = colors.HexColor("#E3EAF5")
ZEBRA = colors.HexColor("#F6F7F9")
CALLOUT = colors.HexColor("#E6ECF7")
AGENT = colors.HexColor("#EEEAFB"); AGENT_LINE = colors.HexColor("#6A5ACD")
KNOW = colors.HexColor("#E2F3EE"); KNOW_LINE = colors.HexColor("#2E8B6E")
HITL = colors.HexColor("#FCEFD9"); HITL_LINE = colors.HexColor("#C98A1B")
IFACE = colors.HexColor("#EFEDE8"); IFACE_LINE = colors.HexColor("#8A8578")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

S = {
    "title": ParagraphStyle("title", fontName="Arial-Bold", fontSize=30, leading=35, textColor=NAVY),
    "subtitle": ParagraphStyle("subtitle", fontName="Arial", fontSize=14, leading=20, textColor=INK),
    "kicker": ParagraphStyle("kicker", fontName="Arial-Bold", fontSize=11, leading=14, textColor=MUTED),
    "h1": ParagraphStyle("h1", fontName="Arial-Bold", fontSize=17, leading=22, textColor=NAVY,
                         spaceBefore=10, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Arial-Bold", fontSize=12.5, leading=16, textColor=INK,
                         spaceBefore=8, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Arial", fontSize=10, leading=14.5, textColor=INK,
                           spaceAfter=6, alignment=TA_LEFT),
    "cell": ParagraphStyle("cell", fontName="Arial", fontSize=8.8, leading=11.5, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Arial-Bold", fontSize=8.8, leading=11.5, textColor=INK),
    "cellh": ParagraphStyle("cellh", fontName="Arial-Bold", fontSize=8.8, leading=11.5, textColor=NAVY),
    "caption": ParagraphStyle("caption", fontName="Arial-Italic", fontSize=8.5, leading=11, textColor=MUTED,
                              spaceBefore=3, spaceAfter=8),
    "callout": ParagraphStyle("callout", fontName="Arial-Bold", fontSize=10.5, leading=15, textColor=NAVY),
    "toc": ParagraphStyle("toc", fontName="Arial", fontSize=10.5, leading=17, textColor=INK),
}


# ---------------------------------------------------------------- building blocks
def P(text, style="body"):
    return Paragraph(text, S[style])


def H1(text):
    return P(text, "h1")


def H2(text):
    return P(text, "h2")


def bullets(items):
    return ListFlowable([ListItem(P(t), leftIndent=12, bulletColor=NAVY) for t in items],
                        bulletType="bullet", start="•", leftIndent=12, bulletFontName="Arial",
                        bulletFontSize=9, spaceAfter=4)


def numbered(items):
    return ListFlowable([ListItem(P(t), leftIndent=14) for t in items], bulletType="1",
                        leftIndent=14, bulletFontName="Arial-Bold", bulletFontSize=9.5, spaceAfter=4)


def table(rows, widths, first_col_bold=True, zebra=True):
    data = []
    for r, row in enumerate(rows):
        cells = []
        for c, val in enumerate(row):
            style = "cellh" if r == 0 else ("cellb" if (c == 0 and first_col_bold) else "cell")
            cells.append(Paragraph(val, S[style]))
        data.append(cells)
    t = Table(data, colWidths=[w * CONTENT_W for w in widths], repeatRows=1)
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 1.1, NAVY),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if zebra:
        for r in range(1, len(rows)):
            if r % 2 == 0:
                ts.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
    t.setStyle(TableStyle(ts))
    return t


def callout(text):
    t = Table([[Paragraph(text, S["callout"])]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CALLOUT),
        ("LINEBEFORE", (0, 0), (0, -1), 4, NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t


def meta_table(rows):
    data = [[Paragraph(k, S["cellb"]), Paragraph(v, S["cell"])] for k, v in rows]
    t = Table(data, colWidths=[0.22 * CONTENT_W, 0.78 * CONTENT_W])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def box(d, x, y, w, h, label, fill, line, sub=None, bold=True):
    d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=fill, strokeColor=line, strokeWidth=0.9))
    ty = y + h / 2 + (3 if sub else -3)
    d.add(String(x + w / 2, ty, label, fontName="Arial-Bold" if bold else "Arial", fontSize=8.2,
                 fillColor=INK, textAnchor="middle"))
    if sub:
        d.add(String(x + w / 2, y + h / 2 - 8, sub, fontName="Arial", fontSize=6.8, fillColor=MUTED,
                     textAnchor="middle"))


def arrow(d, x1, y1, x2, y2, color=MUTED):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=0.9))
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    s = 4.2
    p1 = (x2 - s * math.cos(a - 0.45), y2 - s * math.sin(a - 0.45))
    p2 = (x2 - s * math.cos(a + 0.45), y2 - s * math.sin(a + 0.45))
    d.add(Polygon([x2, y2, p1[0], p1[1], p2[0], p2[1]], fillColor=color, strokeColor=color, strokeWidth=0.5))


def legend(d, y):
    items = [("Agents", AGENT, AGENT_LINE), ("Human in the loop", HITL, HITL_LINE),
             ("Knowledge + storage", KNOW, KNOW_LINE), ("Inputs / interfaces", IFACE, IFACE_LINE)]
    x = 8
    for label, fill, line in items:
        d.add(Rect(x, y, 9, 9, rx=2, ry=2, fillColor=fill, strokeColor=line, strokeWidth=0.8))
        d.add(String(x + 13, y + 1.5, label, fontName="Arial", fontSize=7.5, fillColor=INK))
        x += 13 + pdfmetrics.stringWidth(label, "Arial", 7.5) + 18


# ---------------------------------------------------------------- page templates
def make_doc(path, short_title):
    doc = BaseDocTemplate(path, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=22 * mm,
                          bottomMargin=20 * mm, title=short_title, author="Agentic Dataset Context Engine")
    frame = Frame(MARGIN, 20 * mm, CONTENT_W, PAGE_H - 42 * mm, id="f")

    def cover(c, d):
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H - 28 * mm, PAGE_W, 28 * mm, stroke=0, fill=1)
        c.restoreState()

    def normal(c, d):
        c.saveState()
        c.setFont("Arial", 8)
        c.setFillColor(MUTED)
        c.drawString(MARGIN, PAGE_H - 13 * mm, short_title)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.5)
        c.line(MARGIN, PAGE_H - 15 * mm, PAGE_W - MARGIN, PAGE_H - 15 * mm)
        c.drawRightString(PAGE_W - MARGIN, 11 * mm, f"Page {d.page}")
        c.restoreState()

    doc.addPageTemplates([PageTemplate("cover", [frame], onPage=cover),
                          PageTemplate("normal", [frame], onPage=normal)])
    return doc


def cover_block(kicker, title, subtitle, meta):
    return [Spacer(1, 62 * mm), P(kicker, "kicker"), Spacer(1, 5), P(title, "title"), Spacer(1, 8),
            P(subtitle, "subtitle"), Spacer(1, 18), meta_table(meta), NextPageTemplate("normal"), PageBreak()]


def toc(items):
    return [H1("Contents")] + [P(f"{i}. {t}", "toc") for i, t in enumerate(items, 1)] + [PageBreak()]


# ================================================================= PDF 1: starting stage
def phase1_diagram():
    d = Drawing(CONTENT_W, 320)
    W = CONTENT_W
    # inputs row
    box(d, 0, 274, 150, 38, "Qlik inputs", IFACE, IFACE_LINE, "scripts · logs · QVDs · exports")
    box(d, W - 150, 274, 150, 38, "Dremio", IFACE, IFACE_LINE, "catalog · views · data (read-only)")
    # extraction row
    box(d, 0, 214, 150, 38, "BI extractor", AGENT, AGENT_LINE, "scripts → BI model")
    box(d, 170, 214, 150, 38, "Source resolver", AGENT, AGENT_LINE, "Qlik sources → Dremio")
    box(d, W - 150, 214, 150, 38, "Catalog crawl", AGENT, AGENT_LINE, "datasets · view definitions")
    arrow(d, 75, 274, 75, 252); arrow(d, 150, 233, 170, 233); arrow(d, W - 75, 274, W - 75, 252)
    arrow(d, W - 150, 233, 320, 233)
    # translation row
    box(d, 0, 154, 150, 38, "Script translator", AGENT, AGENT_LINE, "load logic → SQL")
    box(d, 170, 154, 150, 38, "Expression translator", AGENT, AGENT_LINE, "measures → SQL")
    box(d, W - 150, 154, 150, 38, "SQL composer", AGENT, AGENT_LINE, "joins · CTE chains")
    arrow(d, 75, 214, 75, 192); arrow(d, 245, 214, 245, 192); arrow(d, 150, 173, 170, 173)
    arrow(d, 320, 173, W - 150, 173)
    # checking row
    box(d, 0, 94, 150, 38, "Query checker", AGENT, AGENT_LINE, "grain · fan-out · semantics")
    box(d, 170, 94, 150, 38, "Reconciler", AGENT, AGENT_LINE, "runs in Dremio · validates")
    box(d, W - 150, 94, 150, 38, "Review workbench", HITL, HITL_LINE, "ask · queue · approve")
    arrow(d, W - 75, 154, 75, 132); arrow(d, 150, 113, 170, 113); arrow(d, 320, 113, W - 150, 113)
    # knowledge row
    box(d, 0, 36, 320, 36, "Knowledge graph", KNOW, KNOW_LINE, "facts with trust, evidence and history")
    box(d, W - 150, 36, 150, 36, "Migration pack", IFACE, IFACE_LINE, "SQL · lineage · validation")
    arrow(d, 245, 94, 245, 72); arrow(d, W - 75, 94, W - 75, 72)
    legend(d, 2)
    return d


def build_phase1():
    path = os.path.join(OUT, "ADCE - Starting Stage Proposal - Qlik to SQL.pdf")
    doc = make_doc(path, "Agentic Dataset Context Engine  |  Starting stage: Qlik to SQL")
    st = []
    st += cover_block(
        "PROPOSAL · STARTING STAGE",
        "Qlik-to-SQL Reconstruction",
        "The first stage of the Agentic Dataset Context Engine: reconstructing how Qlik reports are "
        "produced, as validated SQL on Dremio.",
        [("Document", "Starting-stage proposal"),
         ("Part of", "Agentic Dataset Context Engine (see the overall proposal)"),
         ("Prepared for", "Data &amp; Engineering Leadership"),
         ("Date", "October 2026"),
         ("Status", "Draft for approval")])
    st += toc(["Summary", "Why start with Qlik", "What the starting stage delivers", "How it works",
               "How results are validated", "The deliverable: a migration pack", "Data protection and environments",
               "Plan and milestones", "Success measures", "What we need to start", "Risks and mitigations",
               "What comes next", "Decision requested"])

    st += [H1("1. Summary"),
           P("Many reports in the organization are produced by Qlik applications whose logic lives in load "
             "scripts, intermediate QVD files and chart expressions. That logic is hard to audit, hard to migrate, "
             "and rarely documented. The data behind it now lives in Dremio."),
           P("We propose to start the Agentic Dataset Context Engine with the capability that proves its central "
             "claim: <b>reconstructing how reported numbers are produced</b>. The starting stage reads Qlik "
             "scripts, translates their logic into <b>Dremio SQL</b>, runs that SQL in Dremio, and checks the "
             "results against independent evidence (reload logs, QVD files and exported reports). Every result "
             "carries an explicit validation level, so nothing is presented as correct without proof."),
           P("Everything the starting stage builds is part of the final product: the knowledge graph, the "
             "review workflow, the Dremio connection and the model gateway are built once, in their final shape, "
             "and extended in later stages."),
           callout("Ask: approve the 12-week starting stage on 2–3 real Qlik applications, with read-only access to "
                   "Dremio and the Qlik artifacts listed in section 10."),
           ]

    st += [H1("2. Why start with Qlik"),
           table([["Reason", "What it means"],
                  ["It proves the headline", "Reconstructing report logic is the product's main promise. Starting "
                   "here proves it in the first weeks rather than at the end."],
                  ["The inputs exist today", "Qlik scripts are available now. Access to Qlik servers can come later "
                   "and is not required to start."],
                  ["Translation is reliable", "Translating real logic is far more deterministic than inferring SQL "
                   "from report outputs, so this is the lowest-risk route to a working end-to-end result."],
                  ["Results can be proven", "Reload logs, QVD files and exported reports give independent ground "
                   "truth without needing the Qlik engine (section 5)."],
                  ["It seeds the knowledge base", "Lineage from scripts and QVD chains, joins from Qlik data models "
                   "and definitions from master items become trusted knowledge for every later stage."],
                  ["It has value on its own", "A validated migration pack per application supports audits, "
                   "decommissioning and migration, even before the rest of the platform exists."]],
                 [0.27, 0.73])]

    st += [H1("3. What the starting stage delivers"),
           P("At the end of 12 weeks:"),
           numbered([
               "Real Qlik applications (QlikView and Qlik Sense) are read from their scripts, including chains of "
               "applications connected through QVD files.",
               "Every Qlik table is reproduced as Dremio SQL, executed in Dremio, and validated as far as the "
               "available evidence allows.",
               "Where measure definitions are available, report figures are reproduced and compared with exported "
               "reports.",
               "All findings are stored as knowledge with evidence and history, and shown as a lineage map from "
               "Dremio datasets through Qlik tables and QVDs to report visuals.",
               "Ambiguities (unresolved variables, uncertain source mappings, statements that could not be "
               "translated) are resolved by reviewers in a workbench, and their decisions are reused.",
               "A migration pack per application can be exported: SQL, lineage, a validation report and a list of "
               "open items.",
               "The same build runs locally and in a staging environment, ready to move into production and into "
               "client environments later."])]

    st += [H1("4. How it works"),
           KeepTogether([phase1_diagram(),
                         P("Figure 1. The starting-stage pipeline. Qlik artifacts and Dremio metadata are combined; "
                           "every result is recorded as knowledge and reviewed before it is trusted.", "caption")]),
           table([["Step", "What happens"],
                  ["1. Intake", "Scripts, QlikView project folders, reload logs, QVD files and exported reports are "
                   "uploaded and classified per application."],
                  ["2. Extract", "Scripts are split into statements and parsed into a tool-neutral model of the "
                   "application: tables, transformations, variables, data model and measures. Nothing is silently "
                   "dropped; anything not understood is flagged."],
                  ["3. Resolve sources", "Each source a script reads is mapped to a Dremio dataset or to the Qlik "
                   "application that produced the QVD file. Uncertain mappings go to review."],
                  ["4. Translate", "Embedded source SQL is converted to the Dremio dialect, and Qlik transformations "
                   "(joins, lookups, concatenations, set analysis) become SQL. AI assistance is used only for "
                   "statements the rules do not cover, and its output is always re-checked."],
                  ["5. Check", "Generated SQL is checked for known Qlik-to-SQL pitfalls, most importantly double "
                   "counting caused by differences in how Qlik and SQL aggregate across joined tables."],
                  ["6. Execute and validate", "SQL runs in Dremio under strict load limits, and results are compared "
                   "with reload logs, QVD files and exported reports (section 5)."],
                  ["7. Review and record", "Reviewers confirm mappings, answer questions and approve SQL. Every "
                   "decision is recorded with its evidence."],
                  ["8. Export", "A migration pack is produced per application (section 6)."]],
                 [0.2, 0.8])]

    st += [H1("5. How results are validated"),
           P("Each reproduced table or figure is labelled with the <b>highest level of evidence it reaches</b>. "
             "Results are never described as correct without a level."),
           table([["Level", "What is checked", "Evidence used"],
                  ["V0 Parsed", "Every script statement is accounted for", "Script text"],
                  ["V1 Checked", "SQL is valid for Dremio and passes the semantic checks", "Rules and knowledge graph"],
                  ["V2 Executes", "SQL runs in Dremio and returns the expected columns", "Dremio"],
                  ["V3 Row counts match", "Rows per table match what Qlik loaded", "Reload logs, QVD file headers"],
                  ["V4 Data matches", "Column-level totals, distinct counts and ranges match", "QVD file contents"],
                  ["V5 Figures match", "Report figures match what users see", "Exported reports or owner-provided figures"],
                  ["V6 Engine match", "Results match the live Qlik engine across several filter selections",
                   "Qlik server (when access is available)"]],
                 [0.2, 0.47, 0.33]),
           Spacer(1, 6),
           P("<b>Point-in-time alignment.</b> Qlik evidence reflects data at the time of the last reload, while "
             "Dremio returns current data. Where Dremio tables support time travel, queries run as of the reload "
             "time and comparisons are exact. Otherwise differences are reported with a stated tolerance and the "
             "time gap is recorded as the reason; such results are never labelled exact.")]

    st += [H1("6. The deliverable: a migration pack"),
           P("For each Qlik application, a versioned pack that can be regenerated identically from the same inputs:"),
           table([["Part", "Contents"],
                  ["SQL", "Dremio SQL for every Qlik table and, where definitions exist, for every measure. Producer "
                   "applications in QVD chains are included, so the SQL reads only Dremio datasets."],
                  ["Lineage", "A map from Dremio datasets to Qlik tables, QVD files, applications and report visuals, "
                   "in a readable summary and in the open OpenLineage format."],
                  ["Validation report", "The level reached by each table and figure, with differences, tolerances "
                   "and their reasons."],
                  ["Review record", "Statements that could not be translated, reviewer decisions applied, and open "
                   "items such as a missing producer application."],
                  ["Manifest", "Which inputs were received and which software and model versions produced the pack."]],
                 [0.22, 0.78])]

    st += [H1("7. Data protection and environments"),
           bullets([
               "<b>Read-only, always.</b> The system reads Dremio and Qlik artifacts. It never writes to Dremio, never "
               "reloads or changes Qlik applications.",
               "<b>No row-level data reaches an AI model in this stage.</b> Models see scripts, schemas, view "
               "definitions and aggregate differences only. This is enforced centrally in the model gateway.",
               "<b>Controlled load on Dremio.</b> Validation queries are bounded, cached, labelled so their load is "
               "visible, limited in concurrency and scheduled off-peak where needed.",
               "<b>Models through Google Cloud.</b> AI models are accessed through Google Cloud (Vertex AI) in an "
               "approved project and region. Client deployments can use the client's own approved endpoint.",
               "<b>Local first, then production.</b> The work starts in local environments with a local copy of "
               "sample data, moves to a staging environment on Google Cloud, and is packaged so it can later be "
               "installed in production or inside a client's own environment.",
               "<b>Full audit trail.</b> Every finding, decision, prompt version and model version is recorded."])]

    st += [H1("8. Plan and milestones"),
           table([["Phase", "Weeks", "Build", "Done when"],
                  ["Foundations", "1–2", "Local environment with Dremio and sample data; shared data formats; "
                   "knowledge store; model gateway; first Qlik script parser on every sample application; readers "
                   "for reload logs and QVD files", "Sample applications are parsed with measured coverage, and a "
                   "first Qlik table runs as SQL in Dremio"],
                  ["Tables", "3–5", "Complete script extraction and translation; source resolution including QVD "
                   "chains; Qlik-to-SQL checks; validation against reload logs and QVD files; review workbench",
                   "At least 90% of script statements translated and at least 80% of tables matching on row counts "
                   "(V3 or higher)"],
                  ["Figures and lineage", "6–8", "Measure translation; reading exported reports; composed SQL per "
                   "visual; lineage map; figure-level validation", "At least three real report figures reproduced "
                   "and matching exported reports (V5)"],
                  ["Harden and package", "9–12", "More applications; complex constructs; migration pack export; "
                   "performance and load tuning; staging environment", "Targets in section 9 met; a migration pack "
                   "accepted by a report owner; the same build running in staging"]],
                 [0.17, 0.08, 0.43, 0.32]),
           Spacer(1, 4),
           P("Progress is reviewed at the end of each phase against these criteria. In parallel, from week 6, the "
             "wider platform (profiling, dataset matching, data dictionary and estate analysis) starts building on "
             "the same foundations; see the overall proposal.")]

    st += [H1("9. Success measures"),
           table([["Measure", "Target by week 12"],
                  ["Script statements translated without being flagged", "At least 90% per application"],
                  ["Tables with matching row counts (V3 or higher)", "At least 80%"],
                  ["Tables with matching data where QVD files exist (V4)", "At least 70%"],
                  ["Report figures matching exported reports (V5), for standard constructs", "At least 70%"],
                  ["Results shown with their validation level", "100%; no unlabelled \"match\""],
                  ["Unresolved variables or sources silently assumed", "None; all go to review"],
                  ["Migration packs regenerated identically from the same inputs", "100%"],
                  ["Cost per application translated", "Measured and reported"]],
                 [0.65, 0.35])]

    st += [H1("10. What we need to start"),
           table([["Input or access", "Why it is needed"],
                  ["2–3 representative Qlik applications: load scripts, QlikView project folders or app files",
                   "The material the pipeline reads, and the basis of the test set"],
                  ["Reload logs for those applications", "Resolve variables and confirm row counts (V3)"],
                  ["QVD files they read and write", "Lineage across applications and data-level checks (V3, V4)"],
                  ["Exported reports or reference figures for 10 or more visuals", "Figure-level validation (V5)"],
                  ["Read-only Dremio access, or a sample dump for local work", "Source resolution, execution and "
                   "validation"],
                  ["Names of the Qlik data connections and what they point to", "Mapping Qlik sources to Dremio"],
                  ["A Google Cloud project with Vertex AI access", "Model access for translation assistance"],
                  ["Review time from a report or data owner", "Confirm mappings and approve reconstructed SQL"]],
                 [0.45, 0.55])]

    st += [H1("11. Risks and mitigations"),
           table([["Risk", "Mitigation"],
                  ["Unusual Qlik syntax the parser does not yet handle",
                   "The grammar grows from real scripts; anything not understood is flagged, never dropped; "
                   "coverage is measured per application from week 1"],
                  ["Only load scripts are provided, without measure definitions",
                   "Table-level SQL and lineage are still delivered; figure-level work starts as soon as project "
                   "folders or app files are available"],
                  ["Data has changed since the last Qlik reload",
                   "Point-in-time queries where available; otherwise stated tolerances with the time gap recorded"],
                  ["Qlik reads sources that are not in Dremio, or reads one of several copies",
                   "Reviewed connection mapping; the reconstruction reproduces exactly what Qlik reads and flags "
                   "where a better (golden) source exists"],
                  ["A producer application in a QVD chain is missing", "The result is marked not resolved and the "
                   "missing application is named in the pack"],
                  ["Validation queries add load to Dremio", "Bounded, cached, labelled and rate-limited queries, "
                   "scheduled off-peak"]],
                 [0.4, 0.6])]

    st += [H1("12. What comes next"),
           P("The starting stage is the first part of the Agentic Dataset Context Engine. From week 6, the wider "
             "platform builds on the same foundations: profiling of Dremio datasets, key and join discovery, "
             "detection of overlapping and duplicate datasets with recommended golden records, the living data "
             "dictionary, and reconstruction of reports for which no Qlik logic is available. Access to the Qlik "
             "servers, when granted, adds live validation against the Qlik engine. At a later stage, everything learned "
             "is used to propose a designed data model (a star schema, a normalized business model or a semantic "
             "layer of curated Dremio views), proven by reproducing existing reports. The overall proposal "
             "describes the complete product and roadmap.")]

    st += [H1("13. Decision requested"),
           callout("Approve the 12-week starting stage: Qlik-to-SQL reconstruction on 2–3 real Qlik applications, with "
                   "read-only Dremio access, the Qlik artifacts in section 10, a Google Cloud project for model "
                   "access, and an infrastructure and model budget to be sized on Google Cloud pricing in the first "
                   "two weeks. Progress is reviewed at the end of each phase against section 9.")]
    doc.build(st)
    return path


# ================================================================= PDF 2: overall
def overall_diagram():
    d = Drawing(CONTENT_W, 330)
    W = CONTENT_W
    g = 8
    third = (W - 2 * g) / 3
    box(d, 0, 292, third, 30, "Review workbench", IFACE, IFACE_LINE, "queue · lineage · dictionary")
    box(d, third + g, 292, third, 30, "API gateway + SSO", IFACE, IFACE_LINE, "access control")
    box(d, 2 * (third + g), 292, third, 30, "External access", IFACE, IFACE_LINE, "MCP · catalog exports")
    box(d, 0, 246, W, 30, "Data sources", IFACE, IFACE_LINE,
        "Dremio (tables, views, view definitions, query history)  ·  Qlik artifacts  ·  documents  ·  files")
    box(d, 0, 200, third, 30, "Model gateway", IFACE, IFACE_LINE, "Google Vertex AI · privacy")
    box(d, third + g, 200, third, 30, "Orchestrator", AGENT, AGENT_LINE, "event-driven runs")
    box(d, 2 * (third + g), 200, third, 30, "Human-in-the-loop service", HITL, HITL_LINE, "ask · queue · gate")
    arrow(d, W / 2, 246, W / 2, 230)
    gw = (W - 5 * g) / 6
    groups = [("Ingestion", "5 agents"), ("Profiling", "5 agents"), ("Comparison", "7 agents"),
              ("Definition", "6 agents"), ("Reverse-SQL", "6 agents"), ("BI adapters", "3 agents")]
    for i, (name, sub) in enumerate(groups):
        x = i * (gw + g)
        box(d, x, 132, gw, 50, name, AGENT, AGENT_LINE, sub)
        d.add(Rect(x + 6, 136, gw - 12, 10, rx=3, ry=3, fillColor=HITL, strokeColor=HITL_LINE, strokeWidth=0.6))
        d.add(String(x + gw / 2, 138.5, "review gate", fontName="Arial", fontSize=6.3, fillColor=INK,
                     textAnchor="middle"))
        arrow(d, x + gw / 2, 132, x + gw / 2, 112)
    arrow(d, W / 2, 200, W / 2, 182)
    box(d, 0, 76, W, 36, "Knowledge graph service", KNOW, KNOW_LINE,
        "typed tools · policy · validation · facts with trust, evidence and history")
    sw = (W - 4 * g) / 5
    stores = [("Fact log", "Postgres"), ("Graph", "Neo4j"), ("Files", "object store"),
              ("Compute", "Dremio · DuckDB"), ("Traces", "Langfuse")]
    for i, (name, sub) in enumerate(stores):
        box(d, i * (sw + g), 22, sw, 34, name, KNOW, KNOW_LINE, sub)
        arrow(d, i * (sw + g) + sw / 2, 76, i * (sw + g) + sw / 2, 56)
    legend(d, 2)
    return d


def build_overall():
    path = os.path.join(OUT, "ADCE - Overall Proposal.pdf")
    doc = make_doc(path, "Agentic Dataset Context Engine  |  Overall proposal")
    st = []
    st += cover_block(
        "PROPOSAL · OVERALL",
        "Agentic Dataset Context Engine",
        "A knowledge-graph-backed, human-supervised agent system that relates datasets, builds a living data "
        "dictionary, and reconstructs how reports are produced.",
        [("Document", "Overall product and architecture proposal (revised with design decisions)"),
         ("Prepared for", "Data &amp; Engineering Leadership"),
         ("Date", "October 2026"),
         ("Status", "Draft for approval"),
         ("Companion", "Starting-stage proposal: Qlik-to-SQL reconstruction")])
    st += toc(["Executive summary", "The problem", "Proposed solution", "Why it is product-worthy",
               "Design principles", "Architecture", "Agent groups", "Knowledge model and trust",
               "Working with the data estate", "Reconstructing reports", "Designing the data model",
               "Human in the loop", "Technology",
               "Security, privacy and deployment", "Roadmap", "Success measures", "Risks and mitigations",
               "Decision requested", "References"])

    st += [H1("1. Executive summary"),
           P("Organizations hold hundreds of datasets whose meaning, keys, relationships and origins are poorly "
             "documented, and many reports whose logic is buried in BI applications. Every integration, migration, "
             "audit or reconciliation starts by rediscovering the same facts, and that knowledge is lost when the "
             "project ends."),
           P("We propose an <b>agentic dataset context engine</b>: specialist AI agents that share one continuously "
             "growing <b>knowledge graph</b>. They read the data estate (Dremio), BI applications (Qlik), schemas and "
             "documents; profile datasets; discover keys and joins; detect overlapping and duplicate datasets and "
             "recommend golden records; draft definitions and lineage; publish a data dictionary; and reconstruct "
             "the SQL behind reports. Statistics answer what they can and AI models handle judgment. Every result "
             "carries evidence and passes human review before it is trusted."),
           P("The value compounds: each dataset, document, report and human correction makes the next analysis "
             "faster and more accurate. Delivery starts with the capability that proves the central claim, "
             "<b>reconstructing Qlik report logic as validated Dremio SQL</b>, and builds the wider platform on the "
             "same foundations."),
           callout("Ask: approve the programme as described, starting with the 12-week Qlik-to-SQL stage, with the "
                   "wider platform building on the same foundations from week 6.")]

    st += [H1("2. The problem"),
           bullets([
               "<b>Knowledge does not compound.</b> Schema matches, keys and join logic are re-derived every time a "
               "dataset is used, with no record of what was learned before.",
               "<b>The data estate has grown without structure.</b> Many views overlap, data is copied, and it is "
               "unclear which dataset is the authoritative (golden) record and which are dumps or snapshots.",
               "<b>Reports cannot be traced.</b> Report logic lives in BI load scripts, intermediate files and chart "
               "expressions. Reconciling a published figure with its source is manual detective work.",
               "<b>Documentation is missing or scattered</b> across people, spreadsheets and old documents, and "
               "arrives late, disconnected from earlier analysis.",
               "<b>Regulatory and migration pressure.</b> Lineage for reported figures must be shown, and legacy BI "
               "applications must be understood before they are migrated or retired."])]

    st += [H1("3. Proposed solution"),
           H2("Inputs"),
           table([["Input", "Examples", "What the system does with it"],
                  ["Data estate", "Dremio tables and views, query history; uploaded files",
                   "Profiles, finds keys, relates datasets, detects duplicates and golden records"],
                  ["BI applications", "Qlik load scripts, project folders, reload logs, QVD files, exported reports",
                   "Extracts report logic and lineage; reconstructs it as SQL and validates it"],
                  ["Context", "DDL, view definitions, data dictionaries, wikis, business documents",
                   "Extracts documented facts and links them to tables and columns"],
                  ["Scope tags", "\"Belongs to source Sales ERP\"",
                   "Attaches new material to a known source so it inherits existing knowledge"]],
                 [0.17, 0.4, 0.43]),
           H2("Outputs"),
           table([["Output", "Format"],
                  ["Dataset profiles, semantic types, sensitivity tags", "Workbench, JSON"],
                  ["Key and join recommendations with evidence", "Workbench, graph"],
                  ["Estate analysis: duplicate clusters, recommended golden records, stale datasets", "Report, graph"],
                  ["Data dictionary and business glossary", "Excel, Markdown, PDF, dbt YAML"],
                  ["Lineage map", "Graph view, OpenLineage"],
                  ["Reconstructed report SQL with validation level", "SQL, migration pack, report"],
                  ["Proposed data model (later stage): star schema, normalized model or curated views", "Model document, diagram, SQL"],
                  ["Knowledge graph access for other tools and AI agents", "MCP server, API"]],
                 [0.65, 0.35])]

    st += [H1("4. Why it is product-worthy"),
           table([["Reason", "Detail"],
                  ["Expensive, recurring problem", "Documenting an undocumented database takes weeks of expert effort; "
                   "one study estimates USD 12,000–48,000 per moderately complex database [1]. It recurs in every "
                   "migration, acquisition, audit and regulatory review."],
                  ["Value compounds (the moat)", "Every confirmed key, definition, join and report mapping becomes a "
                   "trusted fact reused later. After months of use, the knowledge graph is an organization-specific "
                   "asset that cannot be copied."],
                  ["Fills gaps catalogs leave open", "Works where documentation and code are missing; relates "
                   "disconnected sources; reconstructs and validates report logic; handles messy estates with "
                   "duplicates; human-supervised with a full audit trail."],
                  ["Fits existing stacks", "Feeds the catalogs organizations already own (OpenLineage, dbt, MCP) "
                   "instead of replacing them, and deploys inside the client's environment."],
                  ["Clear buyers", "Regulatory reporting lineage, BI and warehouse migration, mergers and acquisitions, "
                   "audit and finance, catalog enrichment."]],
                 [0.27, 0.73]),
           Spacer(1, 6),
           table([["Category", "Examples", "Where we differ"],
                  ["Enterprise data catalogs", "Atlan, Collibra, Alation, Informatica",
                   "They rely on connectors, query logs and existing code; we infer from data and BI logic when "
                   "documentation is missing, and feed their catalogs"],
                  ["Platform-native governance", "Unity Catalog, Snowflake Horizon, Purview, Dataplex",
                   "Limited to one platform; little cross-source reasoning or report reconstruction"],
                  ["Migration and conversion tools", "Commercial BI converters",
                   "Target a specific destination; not connected to a knowledge base; we validate against "
                   "independent evidence"],
                  ["Research systems", "DBAutoDoc [1], LLM-FK [2], Magneto [3]",
                   "Strong single-database methods we build on; no compounding, multi-source product workflow"]],
                 [0.24, 0.3, 0.46])]

    st += [H1("5. Design principles"),
           numbered([
               "<b>Statistics first, AI second.</b> Deterministic checks generate and verify candidates; AI models "
               "adjudicate and explain. Published research shows this combination clearly beats AI alone [1][2][4].",
               "<b>Evidence for everything.</b> Every fact carries its source, evidence, trust level and history. "
               "Nothing is overwritten; superseded facts are kept.",
               "<b>Humans decide what is trusted.</b> High-impact facts (keys, golden records, reconstructed SQL) "
               "require human confirmation, and corrections become reusable examples.",
               "<b>Read-only towards client systems.</b> The system never modifies Dremio or BI applications; it "
               "recommends.",
               "<b>Honest results.</b> Reconstructions report the level of evidence they reach, and \"not resolved\" "
               "is an allowed outcome.",
               "<b>Built to ship.</b> Every external dependency sits behind an interface, so the product runs locally, "
               "in our cloud, or inside a client's environment from the same build."])]

    st += [H1("6. Architecture"),
           KeepTogether([overall_diagram(),
                         P("Figure 1. System architecture. Agents read from and write to one knowledge graph service, "
                           "which enforces provenance, trust and access rules. Every group has a human review gate.",
                           "caption")]),
           table([["Component", "Role"],
                  ["Knowledge graph service", "The only way knowledge is written. About sixteen typed tools (search, "
                   "context packs, lineage, join paths, propose fact, link, feedback), a policy engine, a validator "
                   "that re-checks claims against the data, and context assembly within token budgets."],
                  ["Knowledge core", "Built on our in-house knowledge-graph engine, extended for data-catalog facts: an "
                   "append-only fact log as the system of record, a graph projection for traversal, hybrid search, "
                   "entity resolution and isolation that fails closed."],
                  ["Orchestrator", "Plans runs, reacts to change events so that only affected steps re-run, enforces "
                   "budgets, and pauses for human questions."],
                  ["Model gateway", "Single route to AI models (Google Vertex AI): model routing, structured outputs, "
                   "caching, budgets, response memoization for repeatable results, and the privacy boundary."],
                  ["Human-in-the-loop service", "Questions, review queues and approval gates, stored as data so work "
                   "never blocks on a reviewer."],
                  ["Review workbench", "Review queue, lineage and graph explorer, dictionary view, estate analysis."]],
                 [0.27, 0.73])]

    st += [H1("7. Agent groups"),
           P("Thirty-two agents in five groups plus an orchestrator. Several are deterministic by design: statistics "
             "answer what they can, and models handle judgment."),
           table([["Group", "Agents", "What it does"],
                  ["Ingestion", "Intake router, schema parser, document extractor, entity linker, scope registrar",
                   "Classifies inputs, reads the Dremio catalog and view definitions, extracts documented facts, "
                   "links everything to known sources"],
                  ["Profiling", "Statistics profiler, semantic typer, sensitive-data classifier, key discoverer, "
                   "domain classifier", "Full-data statistics computed inside Dremio, semantic and sensitivity tags, "
                   "primary keys"],
                  ["Comparison and estate", "Candidate finder, match reranker, foreign-key detector, global verifier, "
                   "insight writer, dataset equivalence detector, golden-record scorer",
                   "Relates datasets, finds joins, detects duplicate and overlapping datasets, recommends golden "
                   "records, writes estate insights"],
                  ["Definition and lineage", "Glossary drafter, refinement loop, conflict resolver, lineage inferrer, "
                   "dictionary publisher, data model designer", "Definitions, conflict resolution, lineage, publishing in "
                   "every format, and (later) a designed data model"],
                  ["Reverse-SQL and BI", "Report parser, metric mapper, join planner, SQL synthesizer, query checker, "
                   "reconciler, BI extractor, BI script translator, BI expression translator",
                   "Reconstructs report SQL by translating BI logic or by inferring it from outputs, then validates it"]],
                 [0.18, 0.42, 0.4])]

    st += [H1("8. Knowledge model and trust"),
           P("Sources contain schemas, which contain datasets, which contain columns. Documents describe them; "
             "columns join, match and derive from each other; business terms define them; reports and BI "
             "applications compute metrics from them. Every fact records <b>when it was true in the data</b> and "
             "<b>when the system believed it</b>, so any past state can be reconstructed."),
           table([["Trust level", "Source", "Example"],
                  ["1. Confirmed", "Human decision", "\"order_id + line_no is the key\""],
                  ["2. Documented", "Documents, schemas, view definitions, BI scripts", "Dictionary entry, view SQL, "
                   "Qlik load statement"],
                  ["3. Validated", "Agent finding checked against the full data", "Key proven unique across 1.2M rows"],
                  ["4. Inferred", "Agent reasoning, not yet checked", "Meaning guessed from names and statistics"]],
                 [0.2, 0.4, 0.4]),
           Spacer(1, 4),
           P("When levels disagree (a dictionary says a column is unique, but the data has duplicates), a conflict is "
             "raised for review instead of overwriting either fact. Confirmed facts are never changed, only "
             "superseded by a newer confirmation.")]

    st += [H1("9. Working with the data estate"),
           P("Dremio is the primary source of data and metadata. The system reads it read-only and pushes heavy "
             "computation (statistics, uniqueness and containment checks) down to Dremio, so analysis scales with "
             "the estate rather than with our servers."),
           bullets([
               "<b>Knowledge the estate already holds.</b> View definitions become lineage; joins in views and in "
               "query history become join evidence; wikis and tags become documented context.",
               "<b>Overlapping and duplicate datasets.</b> A layered check finds identical views, views with the same "
               "logic, subsets, snapshots and copies: definitions and lineage first (free), then schemas, then "
               "bounded data fingerprints only where needed. Datasets are grouped into clusters.",
               "<b>Golden records.</b> Each cluster gets a recommended golden record based on lineage position, "
               "completeness, freshness, usage, location and documentation. A data owner always confirms it.",
               "<b>Work per cluster, not per copy.</b> Matching, definitions and the dictionary work on the golden or "
               "best member and apply to its copies, which cuts review effort sharply.",
               "<b>Estate report.</b> Duplicate clusters, recommended golden records, unused datasets and reports that "
               "read a non-golden copy. Recommendations only; the estate is never changed.",
               "<b>Usage first.</b> Analysis starts with what reports and users actually read, then expands."])]

    st += [H1("10. Reconstructing reports"),
           table([["Mode", "When", "How"],
                  ["Translate", "The BI logic is available (Qlik scripts, project folders, QVD files)",
                   "BI logic is extracted into a tool-neutral model and translated into Dremio SQL, then checked and "
                   "executed in Dremio"],
                  ["Infer", "Only the report output is available (PDF, Excel)",
                   "Measures and dimensions are mapped to columns using the knowledge graph; candidate SQL is "
                   "generated and refined until it reproduces the report across several periods"],
                  ["Hybrid", "An exported report belongs to a known BI application",
                   "Translated, and the export is used as additional validation"]],
                 [0.14, 0.33, 0.53]),
           Spacer(1, 6),
           P("Every reconstruction carries a validation level, from \"parsed\" and \"executes\" through \"row counts "
             "match\" and \"data matches\" (against reload logs and QVD files) to \"figures match\" (against exported "
             "reports) and, when server access exists, \"matches the live BI engine\". A known pitfall, double "
             "counting caused by how BI tools and SQL aggregate across joined tables, is checked explicitly. The "
             "first BI adapter covers QlikView and Qlik Sense; the model is tool-neutral so other BI tools can be "
             "added without changing the rest of the system.")]

    st += [H1("11. Designing the data model (later stage)"),
           P("Today the data in Dremio has no designed structure: tables, copies and dumps sit side by side, and the "
             "business logic that turns them into reports is spread across Dremio views, queries and Qlik scripts. "
             "Once the earlier stages have built up confirmed knowledge, the engine can propose the structured layer "
             "the estate is missing. This is <b>later work</b>, after the starting stage and the core platform, and "
             "part of the overall product."),
           table([["Form", "Shape", "Typical use"],
                  ["Star schema", "Fact tables with a declared grain, shared (conformed) dimensions, measures "
                   "defined once", "Reporting and BI"],
                  ["Normalized business model", "Business entities and their relationships, keys and constraints",
                   "Integration and data products"],
                  ["Semantic layer of curated Dremio views", "Business-named views over the golden datasets, with "
                   "documented columns and measures", "Self-service access on top of the existing estate"]],
                 [0.27, 0.43, 0.3]),
           Spacer(1, 6),
           bullets([
               "<b>One logical model, several forms.</b> The engine first builds a single logical model (entities, "
               "keys, relationships, grain, measures) and renders it in whichever forms are chosen per subject area.",
               "<b>Built from what has been learned, not from scratch.</b> Entities come from golden records and "
               "confirmed keys; relationships from confirmed joins, Qlik data models, Dremio views and query "
               "history; transformations and measures from the translated Qlik logic. Logic repeated across many Qlik "
               "applications is consolidated into one shared definition.",
               "<b>Approved by data owners.</b> Entities, grain, keys and relationships pass a review gate before "
               "anything is produced.",
               "<b>Proven against today's reports.</b> Each in-scope report must be reproducible from the model, using "
               "the same validation levels as report reconstruction. A model is not published for a report it cannot "
               "reproduce.",
               "<b>A separate deliverable.</b> The model, its documentation, diagram, measure catalog and SQL are "
               "handed to the client's team to apply. Existing Qlik applications are not changed or re-pointed."])]

    st += [H1("12. Human in the loop"),
           table([["Mode", "When", "Example"],
                  ["Seed", "Before a run", "\"cust_ref in this source is the customer number\""],
                  ["Ask", "An agent cannot proceed", "\"Which value does variable vYear take?\""],
                  ["Gate", "Before a high-impact fact is trusted", "Confirming a key, a golden record or reconstructed SQL"],
                  ["Queue", "Non-blocking batch review", "Approving 30 column matches at once"],
                  ["Correct", "A reviewer edits an output", "Rewriting a definition; the edit becomes confirmed"]],
                 [0.14, 0.33, 0.53]),
           Spacer(1, 4),
           P("Validated, high-confidence, low-impact facts are accepted automatically and stay reversible; uncertain or "
             "high-impact ones are queued or gated. The queue is ordered by impact and uncertainty, so review time "
             "goes where it matters. Corrections are stored with the original proposal and reused as examples, so "
             "agents improve on the organization's data without retraining.")]

    st += [H1("13. Technology"),
           table([["Layer", "Choice"],
                  ["Workbench", "Next.js with React Flow"],
                  ["API and identity", "FastAPI; Keycloak single sign-on, federating to the client's identity provider"],
                  ["Orchestration", "LangGraph with a Postgres checkpointer"],
                  ["Agent-to-knowledge tools", "Model Context Protocol server (FastMCP)"],
                  ["AI models", "Claude models through Google Cloud Vertex AI (Gemini available through the same gateway)"],
                  ["Knowledge store", "Postgres fact log (system of record) with a Neo4j graph projection"],
                  ["Data access", "Dremio via Arrow Flight SQL and its catalog API; DuckDB and Polars for files"],
                  ["SQL and BI", "sqlglot (Dremio dialect); Qlik script grammar; QVD and reload-log readers"],
                  ["Matching", "MinHash sketches, fuzzy matching, statistical containment checks"],
                  ["Privacy", "Microsoft Presidio masking before any model call that includes samples"],
                  ["Observability", "Langfuse and OpenTelemetry"],
                  ["Exports", "OpenLineage, dbt YAML, Excel, Markdown, PDF"],
                  ["Deployment", "Docker Compose locally; Kubernetes with Helm for staging, production and client installs"]],
                 [0.27, 0.73])]

    st += [H1("14. Security, privacy and deployment"),
           bullets([
               "<b>Data minimization.</b> Only schemas, statistics, definitions and masked samples reach an AI model; "
               "raw data stays in the client environment. In the starting stage, no row-level data reaches a model.",
               "<b>Read-only access</b> to Dremio and BI applications through dedicated service accounts, with "
               "controlled query load.",
               "<b>Isolation.</b> Each client's knowledge is isolated in the store itself, and isolation fails closed; "
               "knowledge learned for one client is never used for another.",
               "<b>Access control.</b> Single sign-on with roles for viewers, reviewers, data owners and administrators.",
               "<b>Auditability.</b> An append-only record of every proposal, decision, prompt version and model "
               "version; any past state can be reconstructed.",
               "<b>Environments.</b> Local development first, then staging on Google Cloud, then production. The same "
               "build can run hosted by us in a dedicated project per client, or inside the client's own cloud or "
               "data centre with their approved model endpoint, including air-gapped installations.",
               "<b>Regulation.</b> Designed to support GDPR, India's DPDP Act and sector rules; SOC 2 or ISO 27001 "
               "certification planned before production sales."])]

    st += [H1("15. Roadmap"),
           table([["Stage", "Weeks", "Focus"],
                  ["Starting stage: Qlik to SQL", "1–12", "Qlik scripts reconstructed as validated Dremio SQL, lineage "
                   "across QVD chains, migration packs (see the starting-stage proposal)"],
                  ["Platform: profiling and estate", "6–8", "Dataset profiling in Dremio, sensitive-data tagging, keys; "
                   "first duplicate and overlap detection; usage tracking"],
                  ["Platform: relationships and dictionary", "9–12", "Joins and matches across datasets; golden-record "
                   "recommendations with owner confirmation; first data dictionary; production-shaped installation"],
                  ["Platform: inference and refinement", "13–15", "Reconstruction from report outputs alone; consistency "
                   "checks, definition refinement and conflict resolution; first estate report"],
                  ["Platform: complete and evaluate", "16–18", "All exports, external access for other tools, cost "
                   "dashboards, full evaluation, security review and load testing"],
                  ["Data model design", "After the platform stages", "Logical model for a first subject area, approved by data "
                   "owners; star schema, normalized model and/or curated Dremio views; reports reproduced from "
                   "the model"],
                  ["Pilot", "Following", "Paid pilot with a client on a report family; live Qlik engine validation "
                   "where server access is granted"],
                  ["Product v1 and scale", "Next 6–12 months", "Multi-client hosting, catalog connectors, additional BI "
                   "tools, natural-language questions over the knowledge graph, impact analysis"]],
                 [0.3, 0.14, 0.56]),
           Spacer(1, 4),
           P("Each stage ends with a review against measurable criteria. The go-to-market position: <i>\"We reconstruct "
             "how your numbers are produced when documentation and code are missing, and we feed that knowledge into "
             "the catalog you already own.\"</i>")]

    st += [H1("16. Success measures"),
           table([["Area", "Measure", "Target"],
                  ["Keys", "Primary-key accuracy (F1) on the test set", "At least 90%"],
                  ["Joins", "Foreign-key accuracy (F1) within a source", "At least 80%"],
                  ["Estate", "Precision of identical / same-logic dataset detection", "At least 95%"],
                  ["Estate", "Recommended golden records accepted by owners", "At least 80%"],
                  ["Reconstruction", "BI script statements translated", "At least 90%"],
                  ["Reconstruction", "Report figures reproduced where evidence exists (standard constructs)", "At least 70%"],
                  ["Trust", "Automatically accepted facts later reversed by reviewers", "Below 5%"],
                  ["Compounding", "Review items per new dataset from the same source", "Declining over time"],
                  ["Freshness", "Late-arriving documents update affected facts without manual re-runs", "Always"],
                  ["Data model", "In-scope reports reproduced from the designed model (later stage)", "Set when planned"],
                  ["Cost", "Cost per dataset and per report", "Measured and reported"]],
                 [0.18, 0.57, 0.25])]

    st += [H1("17. Risks and mitigations"),
           table([["Risk", "Mitigation"],
                  ["Wrong facts spread through the knowledge graph", "Trust levels, validation against data, review "
                   "gates, full history and rollback; confirmed facts never change"],
                  ["Duplicate datasets cause repeated work or facts attached to the wrong copy", "Equivalence clusters; "
                   "work on the golden or best member; owners confirm golden records"],
                  ["Reconstructed SQL matches by coincidence", "Validation across several periods, slices and "
                   "independent evidence; validation levels reported"],
                  ["AI output varies between runs", "Structured outputs, pinned prompt and model versions, response "
                   "memoization, regression evaluation before any model change"],
                  ["Load on the client's Dremio", "Bounded and cached queries, concurrency limits, off-peak scheduling, "
                   "visible query labels"],
                  ["Reviewer fatigue", "Impact-ranked queue, batching, per-cluster review, automatic acceptance of "
                   "validated low-impact facts"],
                  ["Cost overruns", "Per-task and per-run budgets, caching, model routing by task difficulty"],
                  ["Overlap with incumbent catalogs", "Positioned as an inference engine that feeds catalogs"]],
                 [0.4, 0.6])]

    st += [H1("18. Decision requested"),
           callout("Approve the Agentic Dataset Context Engine programme as described: the 12-week Qlik-to-SQL starting "
                   "stage, followed by the platform stages through week 18, with read-only access to Dremio and the Qlik "
                   "artifacts, a Google Cloud project for model access, and an infrastructure and model budget to be "
                   "sized on Google Cloud pricing in the first two weeks. Progress is reviewed at the end of every stage "
                   "against section 16.")]

    st += [H1("19. References"),
           P("[1] Nagarajan, A. and Altman, T. DBAutoDoc: Automated Discovery and Documentation of Undocumented "
             "Database Schemas via Statistical Analysis and Iterative LLM Refinement. arXiv:2603.23050, 2026."),
           P("[2] Tang, Z. et al. LLM-FK: Multi-Agent LLM Reasoning for Foreign Key Detection in Large-Scale Complex "
             "Databases. Findings of ACL 2026. arXiv:2603.07278."),
           P("[3] Liu, Y. et al. Magneto: Combining Small and Large Language Models for Schema Matching. PVLDB 18(8), 2025."),
           P("[4] Tripathi, S. et al. Scalable Join Inference for Large Context Graphs. arXiv:2603.04176, 2026."),
           P("[5] Tran, Q. T., Chan, C.-Y. and Parthasarathy, S. Query Reverse Engineering. The VLDB Journal, 2013."),
           P("[6] Sequeda, J. et al. A Benchmark to Understand the Role of Knowledge Graphs on LLM Accuracy for "
             "Question Answering on Enterprise SQL Databases. arXiv:2311.07509, 2023."),
           P("[7] Zep: A Temporal Knowledge Graph Architecture for Agent Memory. arXiv:2501.13956."),
           P("[8] Dremio documentation: Arrow Flight SQL, catalog API, system tables and Iceberg time travel."),
           P("[9] Qlik documentation and community material on QVD file headers and reload logs.")]
    doc.build(st)
    return path


if __name__ == "__main__":
    for p in (build_phase1(), build_overall()):
        print("wrote", p)
