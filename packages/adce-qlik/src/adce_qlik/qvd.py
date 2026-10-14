"""Read the XML header of a QVD file. The binary symbol and index tables are read in P2 (PyQvd / qvdrs)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, Field

END_TAG = b"</QvdTableHeader>"
MAX_HEADER = 64 * 1024 * 1024


class QvdField(BaseModel):
    name: str
    symbols: int | None = None
    tags: list[str] = Field(default_factory=list)


class QvdLineage(BaseModel):
    discriminator: str | None = None
    statement: str | None = None


class QvdHeader(BaseModel):
    table_name: str | None
    creator_doc: str | None
    created_utc: str | None
    records: int | None
    fields: list[QvdField]
    lineage: list[QvdLineage]
    header_bytes: int


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    t = el.find(tag)
    return t.text if t is not None and t.text is not None else None


def parse_qvd_header(data: bytes) -> QvdHeader:
    end = data.find(END_TAG)
    if end < 0:
        raise ValueError("not a QVD file: QvdTableHeader end tag not found")
    xml = data[: end + len(END_TAG)]
    root = ET.fromstring(xml.decode("utf-8", errors="replace").lstrip("﻿"))
    fields = []
    for f in root.iter("QvdFieldHeader"):
        sym = _text(f, "NoOfSymbols")
        fields.append(QvdField(name=_text(f, "FieldName") or "", symbols=int(sym) if sym else None,
                               tags=[t.text for t in f.iter("String") if t.text]))
    lineage = [QvdLineage(discriminator=_text(li, "Discriminator"), statement=_text(li, "Statement"))
               for li in root.iter("LineageInfo")]
    recs = _text(root, "NoOfRecords")
    return QvdHeader(table_name=_text(root, "TableName"), creator_doc=_text(root, "CreatorDoc"),
                     created_utc=_text(root, "CreateUtcTime"), records=int(recs) if recs else None,
                     fields=fields, lineage=lineage, header_bytes=len(xml))


def read_qvd_header(path: str | Path) -> QvdHeader:
    buf = b""
    with open(path, "rb") as fh:
        while END_TAG not in buf:
            chunk = fh.read(1 << 16)
            if not chunk or len(buf) > MAX_HEADER:
                break
            buf += chunk
    return parse_qvd_header(buf)
