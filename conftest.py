"""Repository-wide test configuration."""
import asyncio
import sys


def pytest_asyncio_loop_factories(config, item):
    # psycopg's async connections (LangGraph's AsyncPostgresSaver) need a selector event loop on Windows.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}
