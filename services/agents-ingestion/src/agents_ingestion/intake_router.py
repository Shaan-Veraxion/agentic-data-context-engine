"""Agent 1: Intake router. Classifies the files received for a Qlik application and registers the app's scope."""
from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from adce_core.agent import AgentContext, AgentKind, AgentResult, AgentSpec, BaseAgent
from adce_core.facts import Entity, Evidence, Fact, Trust


class FileClass(StrEnum):
    SCRIPT = "script"
    RELOAD_LOG = "reload_log"
    QVD = "qvd"
    QLIKVIEW_PROJECT = "qlikview_project"
    APP_BINARY = "app_binary"  # .qvf / .qvw: not readable offline; needs server access (Mode A2)
    REPORT_EXPORT = "report_export"
    APP_OBJECTS = "app_objects"  # Qlik Sense app objects (JSON): measures, dimensions, charts, sheets, variables
    CONNECTION_MAP = "connection_map"
    SOURCE_EXTRACT = "source_extract"  # local stand-in data for a logical source: sources/<space>/<...>/<table>.csv
    OTHER = "other"


_SCRIPT_HINT = re.compile(r"(?im)^\s*(\w+:\s*)?(load|sql\s+select|select|set|let|lib\s+connect|store|drop)\b")
_LOG_HINT = re.compile(r"(?i)execution started|lines fetched|execution finished")
_OBJECTS_HINT = re.compile(r'"(qHyperCubeDef|qMeasure|qDim|qInfo|qDefinition)"')


def classify_file(name: str, head: bytes) -> FileClass:
    """Deterministic classification from the file name and its first bytes (never from data rows)."""
    ext = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    norm = name.replace("\\", "/").lower()
    if (norm.startswith("sources/") or "/sources/" in norm) and ext in (".csv", ".parquet"):
        return FileClass.SOURCE_EXTRACT
    if ext == ".qvd" or b"<QvdTableHeader>" in head[:4096]:
        return FileClass.QVD
    if ext in (".qvf", ".qvw"):
        return FileClass.APP_BINARY
    if ext in (".xlsx", ".xls", ".pdf", ".csv") and "connection" not in name.lower():
        return FileClass.REPORT_EXPORT
    text = head[:65536].decode("utf-8", errors="replace")
    if ext == ".json" and _OBJECTS_HINT.search(text):
        return FileClass.APP_OBJECTS
    if ext == ".xml" and ("<QlikView" in text or "-prj" in name.replace("\\", "/")):
        return FileClass.QLIKVIEW_PROJECT
    if "connection" in name.lower() and ext in (".csv", ".json", ".yaml", ".yml", ".txt"):
        return FileClass.CONNECTION_MAP
    if ext == ".log" or _LOG_HINT.search(text):
        return FileClass.RELOAD_LOG
    if ext in (".qvs", ".txt", ".script") or _SCRIPT_HINT.search(text):
        return FileClass.SCRIPT
    return FileClass.OTHER


class IntakeRouter(BaseAgent):
    spec = AgentSpec(number=1, name="intake_router", version="0.1.0", kind=AgentKind.DETERMINISTIC,
                     triggers=["upload.received"], emits=["upload.classified[bi_app]"], writes=["bi_input"],
                     group="ingestion", hitl_modes=["gate"])

    async def run(self, job: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """job: {"app_id": str, "app_name": str?, "files": [{"name": str, "key": str}]}"""
        result = AgentResult()
        app_key = f"app:{ctx.app_id}"
        await self.register(ctx, Entity(key=app_key, type="BIApp", name=job.get("app_name") or ctx.app_id,
                                        attributes={"tool": "qlik"}))
        classified = []
        for f in job["files"]:
            data = ctx.artifacts.get(f["key"])
            cls = classify_file(f["name"], data[:65536])
            sha = hashlib.sha256(data).hexdigest()
            classified.append({"name": f["name"], "key": f["key"], "class": cls.value, "sha256": sha,
                               "bytes": len(data)})
            await self.propose(ctx, Fact(
                kind="bi_input", subject=app_key, value={"file": f["name"], "class": cls.value, "sha256": sha},
                trust=Trust.DOCUMENTED, evidence=[Evidence(kind="upload", ref=f["key"], detail={"sha256": sha})]),
                result)
        result.outputs["files"] = classified
        result.events.append("upload.classified[bi_app]")
        unreadable = [c["name"] for c in classified if c["class"] in (FileClass.APP_BINARY, FileClass.OTHER)]
        result.metrics.update({"files": len(classified), "unreadable": unreadable})
        return result
