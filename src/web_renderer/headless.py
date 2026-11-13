"""Loading and reading a page in the headless Chromium (Playwright), including PDFs, pagination and iframes."""

import re

from .extraction import extract_frame, text_weight
from .page_actions import accept_consent, click_matching, click_rows, content_frames, fill_form, find_next, follow, inline_iframes, scroll, settle

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0 Safari/537.36"


class RenderError(Exception):
    pass


async def read_pdf(context, url, notes):
    from io import BytesIO
    from pypdf import PdfReader
    resp = await context.request.get(url, timeout=60000)
    if resp.status >= 400:
        raise RenderError(f"{url} answered HTTP {resp.status}")
    reader = PdfReader(BytesIO(await resp.body()))
    notes.append(f"PDF with {len(reader.pages)} page(s), text layer extracted with pypdf")
    return "\n\n".join(f"## Seite {i}\n\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1))


async def render_headless(chromium, url, o, notes, links, rows):
    """Load and extract url in the headless Chromium. Returns (title, final_url, body, modes, HTTP status)."""
    sections, status = [], 0
    # ignore_https_errors: many municipal and utility sites serve broken certificate chains, and we only read public pages.
    ctx = await chromium.new_context(locale="de-DE", viewport={"width": 1366, "height": 2000}, user_agent=UA, accept_downloads=False, ignore_https_errors=True)
    try:
        if not o["load_media"]:
            async def skip_media(r):
                await (r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
            await ctx.route("**/*", skip_media)
        page = await ctx.new_page()
        if re.search(r"\.pdf($|\?)", url, re.I):
            body, title, final_url, modes = await read_pdf(ctx, url, notes), url.rsplit("/", 1)[-1], url, ["pdf"]
        else:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=o["timeout"])
            except Exception as e:
                msg = str(e).splitlines()[0]
                if "Download is starting" in msg:
                    body, title, final_url, modes = await read_pdf(ctx, url, notes), url.rsplit("/", 1)[-1], url, ["pdf"]
                    resp = None
                else:
                    raise RenderError(f"could not load {url}: {msg}") from None
            else:
                if resp is not None and (resp.headers.get("content-type") or "").startswith("application/pdf"):
                    body, title, final_url, modes = await read_pdf(ctx, url, notes), url.rsplit("/", 1)[-1], url, ["pdf"]
                else:
                    status = resp.status if resp is not None else 0
                    if resp is not None and resp.status in (404, 410):
                        raise RenderError(f"{url} answered HTTP {resp.status} (page does not exist)")
                    if resp is not None and resp.status >= 400:
                        # guidecom, coveto and Wiesbaden answer 401 and still serve the page; bot walls answer 403 with a block page. Extract either way and let the reader judge.
                        notes.append(f"HTTP {resp.status}: extracted anyway; this may be a login, error or bot-protection page")
                    title, final_url, body, modes = await interact_and_extract(page, o, notes, sections, links, rows)
        if sections:
            nframes = sum(s.startswith("## iframe") for s in sections)
            if len(sections) > nframes:
                notes.append(f"pages: read {len(sections) - nframes} further page(s), each as a section '## Seite N'")
            if nframes:
                notes.append(f"read {nframes} iframe(s) as separate pages; each follows the main page as a section '## iframe N: URL'")
            body = "\n\n---\n\n".join([body, *sections])
    finally:
        await ctx.close()
    return title, final_url, body, modes, status


async def interact_and_extract(page, o, notes, sections, links, rows):
    await settle(page, o["wait"])
    if o["accept_cookies"]:
        await accept_consent(page, o["wait"], notes)
    if o["fill"] or o["select"]:
        await fill_form(page, o, notes)
    if o["wait_for"]:
        try:
            await page.wait_for_selector(o["wait_for"], timeout=o["wait"] * 2)
        except Exception:
            notes.append(f"wait_for {o['wait_for']!r} never appeared")
    if o["scroll"]:
        await scroll(page, o["scroll"], o["wait"], notes)
    if o["expand"]:
        await click_matching(page, page.main_frame, o["expand_pattern"], o["expand"], notes, "expand", o["wait"])
    frames = [] if o["iframes"] == "off" else await content_frames(page, o["max_iframes"], o["iframe_depth"])
    if o["expand"]:
        for n, frame in enumerate(frames, 1):
            await click_matching(page, frame, o["expand_pattern"], o["expand"], notes, f"iframe {n} expand", o["wait"])
    if o["screenshot"]:
        await page.screenshot(path=o["screenshot"], full_page=True)
        notes.append(f"screenshot saved to {o['screenshot']}")
    if o["iframes"] == "inline":
        await inline_iframes(frames, notes)
        frames = []
    title, final_url = await page.title(), page.url
    chosen, body, frame_links = await extract_frame(page.main_frame, o, notes)
    modes = [chosen]
    links += frame_links

    # Numbered or "Weiter" pagination: click to the next page and extract again, until no next control is found or the content repeats.
    for n in range(2, o["pages"] + 1):
        target = await find_next(page.main_frame, o["next_pattern"], n)
        if not target:
            notes.append(f"pages: no control for page {n}; stopped after {n - 1}")
            break
        await follow(page, target, o["wait"])
        pnotes = []
        _, pbody, plinks = await extract_frame(page.main_frame, o, pnotes, header=False)
        if pbody.strip() == body.strip() or pbody in sections:
            notes.append(f"pages: page {n} repeated the previous one; stopped")
            break
        sections.append(f"## Seite {n}\n\n{pbody}")
        links += plinks
    if o["click_rows"]:
        rows += await click_rows(page, o["click_rows"], o["max_rows"], o["wait"], notes)

    for n, frame in enumerate(frames, 1):
        if frame.is_detached():
            continue
        fnotes = []
        try:
            fmode, fbody, frame_links = await extract_frame(frame, o, fnotes, header=False)
        except Exception as e:
            notes.append(f"iframe {n} ({frame.url}) could not be read: {str(e).splitlines()[0]}")
            continue
        notes += [f"iframe {n}: {x}" for x in fnotes]
        links += frame_links
        if text_weight(fbody) < 20:
            notes.append(f"iframe {n} ({frame.url}) holds no text; left out")
            continue
        modes.append(f"iframe {n} {fmode}")
        sections.append(f"## iframe {n}: {frame.url}\n\n{fbody}")
    return title, final_url, body, modes
