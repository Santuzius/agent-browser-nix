"""Rendering one URL: headless Chromium first, the agent Vivaldi after a bot check, then cleaning and cutting into parts. Renderer keeps one Chromium for the whole process."""

import asyncio
import re

from . import agent_vivaldi
from .headless import render_headless
from .markdown import clean_markdown, filter_lines, parts
from .options import normalise


async def render(chromium, url, link_keys=None, **opts):
    """Render one URL. Returns a dict with title, url, mode, notes, content (one part), total_chars, start, end, part, parts, links, rows.

    browser: "headless" (own Chromium), "agent" (the agents' visible Vivaldi, see agent_vivaldi.py) or "auto": headless, and the agent Vivaldi when headless hits a bot check."""
    o, notes = normalise(opts)
    links, rows = [], []
    if o["browser"] == "agent":
        title, final_url, body, modes, links = await agent_vivaldi.render(url, o, notes)
    else:
        base_notes = list(notes)
        title, final_url, body, modes, status = await render_headless(chromium, url, o, notes, links, rows)
        if o["browser"] == "auto" and modes != ["pdf"]:
            if agent_vivaldi.blocked(title, body, status):
                notes[:] = base_notes + [f"headless Chromium got a bot check ({title!r}); read the page in the agent Vivaldi instead"]
                links.clear()
                try:
                    title, final_url, body, modes, links[:] = await agent_vivaldi.render(url, o, notes)
                except Exception as e:
                    notes.append(f"agent browser failed ({str(e).splitlines()[0]}); returning the bot-check page. Try the browser MCP")
    return finish(title, final_url, body, modes, links, rows, o, notes, link_keys)


def finish(title, final_url, body, modes, links, rows, o, notes, link_keys=None):
    """Clean, filter and cut the extracted Markdown into the requested part."""
    body = clean_markdown(body, o["images"], o["full_urls"], notes, final_url, link_keys)
    if o["grep"] or o["exclude"]:
        body = filter_lines(body, o["grep"], o["exclude"], o["context"])
    link_rows = []
    if o["links"] or o["links_only"]:
        rx = re.compile(o["links"] or ".", re.I)
        seen = set()
        for text, href in links:
            if href not in seen and (rx.search(text) or rx.search(href)):
                seen.add(href)
                link_rows.append([text, href])
    if o["links_only"]:
        body = ""
    total = len(body)
    if o["start"] and not o["part"]:
        # Old-style continuation: cut from start, ending at a paragraph break like the parts do.
        start = min(o["start"], total)
        end = start + parts(body[start:], o["max_length"])[0][1]
        part, count = None, None
    else:
        bounds = parts(body, o["max_length"])
        part = min(max(1, o["part"] or 1), len(bounds))
        start, end = bounds[part - 1]
        count = len(bounds)
    return {"title": title, "url": final_url, "mode": ", ".join(modes), "notes": notes, "total_chars": total, "start": start, "end": end, "part": part, "parts": count, "content": body[start:end], "links": link_rows, "rows": rows}


class Renderer:
    """One Chromium for the whole process, started on first use and restarted if it dies, with at most `concurrency` renders at once. Each render gets a fresh context, so cookies and consent clicks never leak between calls."""

    def __init__(self, concurrency=4, link_keys=None):
        self.link_keys = link_keys
        self.pw = self.browser = None
        self.lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(concurrency)

    async def get(self):
        async with self.lock:
            if self.browser is None or not self.browser.is_connected():
                from playwright.async_api import async_playwright
                if self.pw is None:
                    self.pw = await async_playwright().start()
                self.browser = await self.pw.chromium.launch(args=["--disable-gpu", "--disable-dev-shm-usage"])
            return self.browser

    async def render(self, url, **opts):
        async with self.sem:
            return await render(await self.get(), url, self.link_keys, **opts)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
