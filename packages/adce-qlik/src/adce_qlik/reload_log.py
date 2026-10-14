"""Parse Qlik reload logs (Qlik Sense and QlikView formats).

What is extracted:
- echo lines: script lines as executed, each with the script line number Qlik prints (variables already expanded)
- fetch counts: "N lines fetched" (or "Lines fetched: N"), attributed to the label most recently echoed and to the
  last echoed line number, so the BI IR builder can attach them to the right statement by span
- "<table> << <source> [N lines fetched]" progress lines (QlikView style)
- start/finish markers and error lines
Qlik Cloud logs are summarized since Feb 2026; they parse with fewer echo lines and the same counts.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

TS_SENSE = re.compile(r"^(?P<ts>\d{8}T\d{6}(?:\.\d+)?(?:[+-]\d{4}|Z)?)\s+")
TS_VIEW = re.compile(r"^(?P<ts>\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\s+\d{1,2}:\d{2}:\d{2}(?:\s?[AaPp][Mm])?):?\s+")
LINE_NO = re.compile(r"^(?P<no>\d{4,5})\s+(?P<rest>.*)$")
_NUM = r"\d[\d,.   ]*\d|\d"
FETCHED = re.compile(rf"(?i)^(?P<n>{_NUM})\s+lines\s+fetched")
FETCHED_ALT = re.compile(rf"(?i)^lines\s+fetched\s*:\s*(?P<n>{_NUM})")
PROGRESS = re.compile(rf"(?i)^(?P<table>.+?)\s+<<\s+(?P<source>.+?)(?:\s+(?P<n>{_NUM})\s+lines\s+fetched)?$")
ERROR = re.compile(r"(?i)^(error\b|script error\b|.*\berror:)")
LABEL = re.compile(r"""^\s*(?P<label>\[[^\]]+\]|"[^"]+"|[^\s:;\[\]'"(),=$]+)\s*:(?![:/=\\])""")


class EchoLine(BaseModel):
    line_no: int
    text: str
    timestamp: str | None = None


class Fetch(BaseModel):
    lines: int
    table: str | None = None
    source: str | None = None
    line_no: int | None = None  # last echoed script line before the count
    timestamp: str | None = None


class ReloadLog(BaseModel):
    started_at: str | None = None
    finished_at: str | None = None
    echo: list[EchoLine] = Field(default_factory=list)
    fetches: list[Fetch] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def text_for_lines(self, start: int, end: int) -> str | None:
        parts = [e.text for e in self.echo if start <= e.line_no <= end]
        return "\n".join(parts) if parts else None

    def fetched_for_table(self, table: str) -> list[int]:
        return [f.lines for f in self.fetches if (f.table or "").lower() == table.lower()]


def strip_label(text: str) -> str:
    if re.match(r"(?i)\s*(set|let)\s", text):
        return text
    m = LABEL.match(text)
    return text[m.end():].lstrip() if m else text


def _count(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def parse_reload_log(text: str) -> ReloadLog:
    log = ReloadLog()
    label: str | None = None
    last_line: int | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        ts = None
        m = TS_SENSE.match(line) or TS_VIEW.match(line)
        if m:
            ts = m.group("ts")
            line = line[m.end():]
        lm = LINE_NO.match(line)
        if lm:
            no, rest = int(lm.group("no")), lm.group("rest").rstrip()
            log.echo.append(EchoLine(line_no=no, text=rest, timestamp=ts))
            last_line = no
            lab = LABEL.match(rest)
            if lab and not re.match(r"(?i)\s*(set|let)\s", rest):
                label = lab.group("label").strip('[]"')
            continue
        body = line.strip()
        low = body.lower()
        if low.startswith("execution started"):
            log.started_at = ts
        elif low.startswith("execution finished"):
            log.finished_at = ts
        elif ERROR.match(body):
            log.errors.append(body)
        elif pm := PROGRESS.match(body):
            if pm.group("n"):
                log.fetches.append(Fetch(lines=_count(pm.group("n")), table=pm.group("table").strip(),
                                         source=pm.group("source").strip(), line_no=last_line, timestamp=ts))
            label = pm.group("table").strip()
        elif fm := (FETCHED.match(body) or FETCHED_ALT.match(body)):
            log.fetches.append(Fetch(lines=_count(fm.group("n")), table=label, line_no=last_line, timestamp=ts))
            label = None  # the next count belongs to the next labelled statement
    return log
