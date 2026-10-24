"""Conversation-owned MCP client; Chromium policy lives in mcp_browser.mjs.

The SDK's AnyIO contexts must be entered/exited in the same task. One owner task
serializes commands, while voice cancellation closes the entire conversation's
MCP process instead of leaving a browser action running in the background.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import timedelta
from pathlib import Path

from browser_tool import BROWSE_TIMEOUT, BrowseError, _SLOTS, public_address, public_url

ROOT = Path(__file__).resolve().parent
MCP_CATALOG: list[dict] = []
SAFE_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_type", "browser_hover", "browser_wait_for", "browser_close",
}


class MCPConnection:
    server_script = ROOT / "mcp_browser.mjs"

    def __init__(self, host: str = "", address: str = "") -> None:
        self.host = host
        self.address = address
        self.task = None
        self.queue = asyncio.Queue()
        self.ready = None
        self.inflight = None
        self.closing = False

    async def start(self) -> list[dict]:
        if self.task is None:
            self.ready = asyncio.get_running_loop().create_future()
            self.task = asyncio.create_task(self._owner())
        return await asyncio.shield(self.ready)

    async def _owner(self) -> None:
        pending = None
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from playwright.async_api import async_playwright

            # Reuse the full Chromium installed/pinned by the Python bootstrap.
            async with async_playwright() as playwright:
                executable = playwright.chromium.executable_path
            params = StdioServerParameters(
                command="node", args=[str(self.server_script)], cwd=ROOT,
                # Do not pass HF tokens, .env secrets or ambient proxy settings.
                env={"DOT_MCP_HOST": self.host, "DOT_MCP_ADDRESS": self.address,
                     "DOT_MCP_CHROMIUM": executable},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=BROWSE_TIMEOUT)) as session:
                    await session.initialize()
                    catalog = []
                    cursor = None
                    while True:
                        result = await session.list_tools(cursor=cursor)
                        catalog.extend({"type": "function", "function": {
                            "name": tool.name, "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        }} for tool in result.tools)
                        cursor = result.nextCursor
                        if not cursor:
                            break
                    self.ready.set_result(catalog)
                    while not self.closing:
                        command = await self.queue.get()
                        if command is None:
                            break
                        name, arguments, pending = command
                        try:
                            self.inflight = asyncio.create_task(session.call_tool(name, arguments))
                            result = await self.inflight
                            text = "\n".join(item.text for item in result.content if item.type == "text")
                            if not pending.done():
                                pending.set_result(text)
                        except asyncio.CancelledError:
                            if not self.closing:
                                raise
                            break
                        except Exception as exc:
                            if not pending.done():
                                pending.set_exception(BrowseError(f"MCP: {exc}"))
                        finally:
                            self.inflight = None
                            pending = None
        except BaseException as exc:
            error = BrowseError(f"no se pudo mantener Playwright MCP: {type(exc).__name__}: {exc}")
            if not self.ready.done():
                self.ready.set_exception(error)
            if pending is not None and not pending.done():
                pending.set_exception(error)
        finally:
            while not self.queue.empty():
                command = self.queue.get_nowait()
                if command is not None and not command[2].done():
                    command[2].set_exception(BrowseError("conexión MCP cerrada"))

    async def call(self, name: str, arguments: dict) -> str:
        await self.start()
        if self.task.done():
            raise BrowseError("conexión MCP cerrada; abre de nuevo la página")
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((name, arguments, future))
        try:
            async with asyncio.timeout(BROWSE_TIMEOUT + 3):
                return await future
        except (asyncio.CancelledError, TimeoutError):
            await self.close(cancel=True)
            raise

    async def close(self, *, cancel: bool = False) -> None:
        if self.task is None:
            return
        self.closing = True
        if cancel and self.inflight:
            self.inflight.cancel()
        self.queue.put_nowait(None)
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=5)
        except TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None


async def discover_tools() -> list[dict]:
    """Discover all native tools without launching Chromium or visiting a site."""
    connection = MCPConnection()
    try:
        async with asyncio.timeout(20):
            catalog = await connection.start()
        MCP_CATALOG[:] = catalog
        return catalog
    finally:
        await connection.close()


def browser_specs() -> list[dict]:
    """Full-catalog experiment changes discovery, never execution permissions."""
    all_tools = os.environ.get("DOT_MCP_ALL_TOOLS", "0") == "1"
    return [spec for spec in MCP_CATALOG if all_tools or spec["function"]["name"] in SAFE_TOOLS]


class MCPBrowserSession:
    def __init__(self) -> None:
        self.connection = None
        self.host = None
        self.leased = False
        self.lock = asyncio.Lock()

    async def _reset(self, cancel=False) -> None:
        try:
            if self.connection:
                await self.connection.close(cancel=cancel)
        finally:
            self.connection = None
            self.host = None
            if self.leased:
                self.leased = False
                _SLOTS.release()

    async def close(self) -> str:
        async with self.lock:
            await self._reset()
        return "Navegador cerrado."

    async def call(self, name: str, arguments: dict) -> str:
        if name not in SAFE_TOOLS:
            raise BrowseError(f"herramienta bloqueada por seguridad: {name}")
        if name == "browser_close":
            return await self.close()
        async with self.lock:
            try:
                async with asyncio.timeout(BROWSE_TIMEOUT + 5):
                    if name == "browser_navigate":
                        url, host = public_url(arguments.get("url", ""))
                        arguments = {**arguments, "url": url}
                        if host != self.host or self.connection is None:
                            await self._reset()
                            address = await public_address(host)
                            await _SLOTS.acquire()
                            self.leased = True
                            self.host = host
                            self.connection = MCPConnection(host, address)
                    if self.connection is None:
                        raise BrowseError("abre primero una página con browser_navigate(url)")
                    return await self.connection.call(name, arguments)
            except asyncio.CancelledError:
                await self._reset(cancel=True)
                raise
            except TimeoutError as exc:
                await self._reset(cancel=True)
                raise BrowseError("Playwright MCP agotó el tiempo de espera") from exc
            except BrowseError:
                await self._reset(cancel=True)
                raise

    async def navigate(self, url: str) -> str:
        return await self.call("browser_navigate", {"url": url})

    async def snapshot(self) -> str:
        return await self.call("browser_snapshot", {})

    async def click(self, ref: str) -> str:
        return await self.call("browser_click", {"target": ref})

    async def type(self, ref: str, text: str) -> str:
        return await self.call("browser_type", {"target": ref, "text": text})
