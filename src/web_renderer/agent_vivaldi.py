"""Reading a page in the agent Vivaldi (profile "Agent", started by `agent-browser`) over the Chrome DevTools Protocol.

Why: headless Chromium does not pass Cloudflare's bot check; Geizhals answered "Nur einen Moment…" to every headless call on 2026-11-13, while the same page opened at once in the visible Vivaldi. Why raw CDP instead of Playwright: Playwright's connect_over_cdp hangs on Vivaldi (a page target never answers Page.enable), whereas plain Target.createTarget/Runtime.evaluate work.

The page opens as the active tab, so the user can watch in the agent Vivaldi, is read with the same extractors as in extraction.py and is closed again. Load-more buttons and scrolling work; pagination, forms, click_rows and iframes do not (a note says so).
"""

import asyncio
import itertools
import json
import os
import re
import shutil
import subprocess
import urllib.request

import websockets

from .extraction import EXTRACT, FLATTEN_SHADOW, HEADER, LINKS, MARK_HIDDEN, NOISE_SELECTOR_RE, extractor_scripts, pick, text_weight, with_header

PORT = int(os.environ.get("AGENT_BROWSER_PORT", "9333"))
BOT_TITLE_RE = re.compile(r"just a moment|nur einen moment|einen moment bitte|attention required|checking your browser|verify(ing)? you are human|access denied|zugriff verweigert", re.I)

# Same order as page_actions.CLICKABLE: real controls before li/span. Returns the clicked element's text, or null.
CLICK_MATCHING = """(pattern) => {
  const re = new RegExp(pattern, 'i');
  for (const sel of ['a, button, [role=button], [role=link], input[type=button], input[type=submit]', 'li, span']) {
    for (const el of document.querySelectorAll(sel)) {
      const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
      if (re.test(t) && el.getClientRects().length) { el.scrollIntoView({block: 'center'}); el.click(); return t; }
    }
  }
  return null;
}"""


def ws_url():
    """DevTools WebSocket URL of the agent browser, starting it first if it is not running."""
    def version():
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as r:
            return json.load(r)["webSocketDebuggerUrl"]
    try:
        return version()
    except OSError:
        launcher = os.environ.get("AGENT_BROWSER") or shutil.which("agent-browser")
        launcher = [launcher] if launcher else ["bash", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "agent-browser.sh")]
        subprocess.run(launcher, check=True, timeout=30, capture_output=True)
        return version()


class Tab:
    """One tab, driven over a flat CDP session."""

    def __init__(self, ws):
        self.ws, self.ids, self.waiting, self.session, self.target = ws, itertools.count(1), {}, None, None
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            fut = self.waiting.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_exception(RuntimeError(msg["error"].get("message", str(msg["error"])))) if "error" in msg else fut.set_result(msg.get("result", {}))

    async def send(self, method, params=None, session=True, timeout=30):
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.waiting[i] = fut
        msg = {"id": i, "method": method, "params": params or {}}
        if session and self.session:
            msg["sessionId"] = self.session
        await self.ws.send(json.dumps(msg))
        return await asyncio.wait_for(fut, timeout)

    async def open(self):
        self.target = (await self.send("Target.createTarget", {"url": "about:blank", "background": False}, session=False))["targetId"]
        self.session = (await self.send("Target.attachToTarget", {"targetId": self.target, "flatten": True}, session=False))["sessionId"]

    async def close(self):
        if self.target:
            try:
                await self.send("Target.closeTarget", {"targetId": self.target}, session=False, timeout=5)
            except Exception:
                pass
        self.reader.cancel()

    async def eval(self, fn, *args, timeout=60):
        """Call a JavaScript function source with JSON arguments and return its value."""
        expr = f"({fn})({', '.join(json.dumps(a) for a in args)})"
        r = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout=timeout)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"].get("text"))
        return r.get("result", {}).get("value")

    async def settle(self, ms):
        """Wait until the document has loaded and its text has stopped growing for about a second, at most ms."""
        last, stable = -1, 0
        for _ in range(max(4, ms // 250)):
            await asyncio.sleep(0.25)
            try:
                state, size = await self.eval("() => [document.readyState, document.body ? document.body.innerText.length : 0]", timeout=5)
            except Exception:
                continue
            stable = stable + 1 if state == "complete" and size == last else 0
            last = size
            if stable >= 4:
                return


async def render(url, o, notes):
    """Open url in the agent Vivaldi and extract it. Returns (title, final_url, body, modes, links) like headless.interact_and_extract."""
    unsupported = [k for k in ("pages", "fill", "select", "click_rows") if o.get(k)]
    if unsupported:
        notes.append(f"agent browser: {', '.join(unsupported)} not supported there; ignored")
    async with websockets.connect(await asyncio.to_thread(ws_url), max_size=None, open_timeout=15) as ws:
        tab = Tab(ws)
        try:
            await tab.open()
            await tab.send("Page.navigate", {"url": url})
            await tab.settle(o["wait"])
            for _ in range(o["scroll"]):
                await tab.eval("() => window.scrollTo(0, document.body.scrollHeight)")
                await tab.settle(o["wait"])
            clicks = 0
            while clicks < o["expand"]:
                before = await tab.eval("() => [location.href, document.body.innerText.length]")
                if not await tab.eval(CLICK_MATCHING, o["expand_pattern"]):
                    break
                clicks += 1
                await tab.settle(o["wait"])
                href, size = await tab.eval("() => [location.href, document.body.innerText.length]")
                if href.split("#")[0] != before[0].split("#")[0]:
                    await tab.eval("() => history.back()")
                    await tab.settle(o["wait"])
                    notes.append("expand: a matching link navigated away, went back and stopped")
                    break
                if size <= before[1] + 40:
                    notes.append(f"expand: click {clicks} added no text; stopped")
                    break
            if clicks:
                notes.append(f"expand: clicked {clicks}x")
            if not o["no_details"]:
                await tab.eval("() => document.querySelectorAll('details:not([open])').forEach(d => d.open = true)")
            await tab.eval(MARK_HIDDEN)
            if not o["no_shadow"]:
                shadows = await tab.eval(FLATTEN_SHADOW)
                if shadows:
                    notes.append(f"flattened {shadows} shadow root(s)")
            for script in extractor_scripts():
                await tab.eval(script)
            res = await tab.eval(EXTRACT, [o["mode"], o["keep_nav"], NOISE_SELECTOR_RE])
            chosen, body = pick(res, o["mode"], notes)
            body = with_header(body, await tab.eval(HEADER), chosen, o, notes)
            title, final_url = await tab.eval("() => [document.title, location.href]")
            links = await tab.eval(LINKS) if (o["links"] or o["links_only"]) else []
        finally:
            await tab.close()
    return title, final_url, body, [f"agent browser {chosen}"], links


def blocked(title, body, status):
    """True if the page is a bot check or block page rather than content."""
    small = text_weight(body) < 400
    return bool(BOT_TITLE_RE.search(title or "")) and small or (status == 403 and small) or bool(re.search(r"cf-turnstile|challenges\.cloudflare\.com|enable javascript and cookies to continue", body, re.I)) and small
