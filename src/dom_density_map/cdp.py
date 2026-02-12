"""Minimal CDP (Chrome DevTools Protocol) client over websocket.

Only the subset needed for DOM density mapping — no Chrome launching,
no profile management, no TikTok/Reddit specifics.
"""

import json
import asyncio
import urllib.request

import websockets


class CDP:
    """Minimal CDP client over websocket."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self._id = 0

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=50 * 1024 * 1024)
        await self.send("Page.enable")

    async def send(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await self.ws.recv())
            # Auto-dismiss browser dialogs (beforeunload, alerts, etc.)
            if resp.get("method") == "Page.javascriptDialogOpening":
                self._id += 1
                await self.ws.send(json.dumps({
                    "id": self._id,
                    "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": True},
                }))
                continue
            if resp.get("id") == msg["id"]:
                return resp.get("result", {})

    async def navigate(self, url: str, wait: float = 5):
        """Navigate to a URL, disabling beforeunload first."""
        await self.execute_js("window.onbeforeunload = null")
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(wait)

    async def execute_js(self, expression: str) -> dict:
        """Run JavaScript in the page and return the result by value."""
        return await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })

    async def close(self):
        if self.ws:
            await self.ws.close()


def get_ws_url(port: int = 9222) -> str:
    """Get the websocket URL for the first page tab on the given CDP port."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json")
    with urllib.request.urlopen(req, timeout=3) as resp:
        tabs = json.loads(resp.read())
    for tab in tabs:
        if tab.get("type") == "page":
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("No page tab found in Chrome DevTools")


def is_chrome_running(port: int = 9222) -> bool:
    """Check if Chrome is reachable on the given CDP port."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False
