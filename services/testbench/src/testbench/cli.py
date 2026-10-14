"""adce-bench: generate test cases, run them from the terminal, or open the test bench in a browser.

    uv run adce-bench generate                      write the catalogue's cases to eval/cases
    uv run adce-bench list                          cases, what they cover, their variants
    uv run adce-bench run [filter] [--agents 26] [--ai replay] [--data no_qvd] [--all-variants] [-v]
    uv run adce-bench serve [--port 8765]           the test bench page (http://127.0.0.1:8765)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .cases import AI_MODES, DATA_VARIANTS, all_cases
from .runner import Variant, default_variants, run_case

MARK = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "xfail": "xfail", "xpass": "XPASS"}


def cmd_generate(args: argparse.Namespace) -> int:
    from .generate import generate

    for d in generate():
        print(d.relative_to(d.parents[2]).as_posix())
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for c in all_cases():
        print(f"{c.id:45} {c.status:9} chain={c.chain} ai={c.variants.get('ai')} data={c.variants.get('data')}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cases = [c for c in all_cases() if not args.filter or args.filter in c.id]
    agents = [int(a) for a in args.agents.split(",")] if args.agents else None
    totals: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for c in cases:
        if args.all_variants:
            variants = [Variant(ai, d) for ai in AI_MODES[:2] for d in DATA_VARIANTS]
        elif args.ai or args.data:
            variants = [Variant(args.ai or "off", args.data or "full")]
        else:
            variants = default_variants(c)
        for v in variants:
            r = asyncio.run(run_case(c, v, agents))
            totals[r.status] = totals.get(r.status, 0) + 1
            print(f"{MARK[r.status]:6} {c.id:45} {v.label():30} {r.seconds:5.1f}s", flush=True)
            if args.verbose or r.status in ("fail", "error", "xpass"):
                for s in r.steps:
                    if s["error"]:
                        print(f"         agent {s['number']} {s['name']}: {s['error']}")
                for ch in r.checks:
                    if not ch.ok or args.verbose:
                        mark = "ok " if ch.ok else "BAD"
                        print(f"         {mark} [{ch.section}] {ch.name}: expected {ch.expected!r}, got {ch.actual!r}")
                for t, view in r.rows.items():
                    if not view["ok"] and view.get("missing") is not None:
                        print(f"         rows {t}: missing {view['missing'][:5]} extra {view['extra'][:5]}")
            rows.append(r.to_dict())
    print(" ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1, default=str)
    return 1 if totals.get("fail") or totals.get("error") or totals.get("xpass") else 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import app

    print(f"ADCE test bench on http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adce-bench", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate").set_defaults(fn=cmd_generate)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    r = sub.add_parser("run")
    r.add_argument("filter", nargs="?")
    r.add_argument("--agents", help="comma-separated agent numbers (prerequisites are added); 0 = orchestrator")
    r.add_argument("--ai", choices=AI_MODES)
    r.add_argument("--data", choices=DATA_VARIANTS)
    r.add_argument("--all-variants", action="store_true", help="every data variant, with AI off and replay")
    r.add_argument("--json", help="write the results to this file")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
