"""Run the API gateway:  uv run python -m api_gateway [--port 8700]

Uses a selector event loop on Windows (psycopg's async connections, used by the LangGraph checkpointer, need it).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8700)
    a = p.parse_args()
    server = uvicorn.Server(uvicorn.Config("api_gateway.app:app", host=a.host, port=a.port, log_level="info"))
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
