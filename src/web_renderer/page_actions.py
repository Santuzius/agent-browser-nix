"""Actions on a Playwright page before extraction: waiting, clicking load-more and next-page controls, consent banners, forms, scrolling, iframes and rows without links."""

import re

from .extraction import MARK_HIDDEN

CONSENT_RE = r"^\s*(alle )?(akzeptieren|annehmen|zustimmen|alle zulassen|alle cookies (akzeptieren|zulassen)|einverstanden|accept( all)?( cookies)?|allow all|agree|ok|verstanden)\s*$"
IFRAME_SKIP_RE = re.compile(r"youtube|vimeo|google|maps|recaptcha|hcaptcha|challenges\.cloudflare|turnstile|cookie|consent|facebook|twitter|instagram|doubleclick|matomo|analytics", re.I)


async def settle(page, ms):
    try:
        await page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass  # pages with polling or analytics never go idle; carry on with what has loaded


async def text_len(frame):
    try:
        return await frame.evaluate("() => document.body ? document.body.innerText.length : 0")
    except Exception:
        return 0


async def dom_click(el):
    """Playwright's click waits for the element to be visible, stable and unobstructed. That never happens inside iframes that keep resizing (Jobcluster) or under a cookie backdrop (interamt), so fall back to a plain DOM click, which needs no layout."""
    try:
        await el.scroll_into_view_if_needed(timeout=2000)
        await el.click(timeout=3000)
    except Exception:
        await el.evaluate("e => e.click()")


CLICKABLE = ["a, button, [role=button], [role=link], input[type=button], input[type=submit]", "li, span"]


async def find_clickable(frame, pattern):
    """First visible element whose own text matches pattern. Real controls are searched before li/span, because a DOM click on the <li> around a pagination link does nothing."""
    for selector in CLICKABLE:
        loc = frame.locator(selector).filter(has_text=re.compile(pattern, re.I))
        for i in range(min(await loc.count(), 40)):
            el = loc.nth(i)
            try:
                text = ((await el.inner_text(timeout=500)) or (await el.get_attribute("value")) or (await el.get_attribute("aria-label")) or "").strip()
                if re.match(pattern, text, re.I) and await el.is_visible():
                    return el
            except Exception:
                continue
    return None


NEXT_SELECTORS = "a[rel=next], [aria-label*=next i], [aria-label*=weiter i], [aria-label*=nächste i], [title*=nächste i], [title*='next page' i], .next > a, a.next, li.next a, .pagination-next a, a.pagination-next"


async def find_next(frame, pattern, n):
    """The control that leads to page n: an element marked for page n (data-page), rel=next or a next/weiter label (arrows are often icon-only), then a 'Weiter'/'›' text, then the button labelled n."""
    loc = frame.locator(f"[data-page='{n}'], [data-page-number='{n}'], [aria-label='Seite {n}' i], [aria-label='Page {n}' i], " + NEXT_SELECTORS)
    for i in range(min(await loc.count(), 10)):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                return el
        except Exception:
            continue
    return await find_clickable(frame, pattern) or await find_clickable(frame, rf"^\s*{n}\s*$")


async def follow(page, el, wait_ms):
    """Open a link by navigating to its href when it has a real one (robust against overlays and click handlers that ignore synthetic clicks), otherwise click it and wait until the page text changes. AJAX pagination (Vermögen und Bau BW) fires its request after the click returns, so "network idle" alone came too early and the old page was read twice."""
    href = await el.get_attribute("href") if await el.evaluate("e => e.tagName") == "A" else None
    if href and not href.startswith(("#", "javascript:")):
        await page.goto(await el.evaluate("e => e.href"), wait_until="domcontentloaded")
        await settle(page, wait_ms)
        return
    before = await page.evaluate("() => document.body ? document.body.innerText : ''")
    await dom_click(el)
    for _ in range(max(1, wait_ms // 250)):
        await page.wait_for_timeout(250)
        if await page.evaluate("() => document.body ? document.body.innerText : ''") != before:
            break
    await settle(page, wait_ms)


async def click_matching(page, frame, pattern, limit, notes, label, wait_ms):
    """Click elements whose text matches pattern, up to limit times. Stops when a click navigates away or adds no text: an "expand" button that loads nothing is a carousel or a toggle, not a list (interamt's home page got ten pointless clicks this way)."""
    clicks = 0
    start_url = frame.url
    while clicks < limit:
        target = await find_clickable(frame, pattern)
        if not target:
            break
        before = await text_len(frame)
        try:
            await dom_click(target)
        except Exception as e:
            notes.append(f"{label}: could not click: {str(e).splitlines()[0]}")
            break
        clicks += 1
        await settle(page, wait_ms)
        if frame.is_detached() or frame.url.split("#")[0] != start_url.split("#")[0]:
            if frame is page.main_frame:
                await page.go_back()
                await settle(page, wait_ms)
                notes.append(f"{label}: a matching link navigated away, went back and stopped")
            else:
                notes.append(f"{label}: a matching link navigated the iframe away; stopped")
            break
        if await text_len(frame) <= before + 40:
            notes.append(f"{label}: click {clicks} added no text; stopped")
            break
    if clicks:
        notes.append(f"{label}: clicked {clicks}x")
    return clicks


async def accept_consent(page, wait_ms, notes):
    for frame in page.frames:
        el = await find_clickable(frame, CONSENT_RE)
        if el:
            try:
                await dom_click(el)
                await settle(page, wait_ms)
                notes.append("consent: clicked")
                return
            except Exception:
                continue
    notes.append("consent: no consent button found")


async def fill_form(page, o, notes):
    """Type into fields and pick options, then optionally submit. Selectors are CSS or Playwright selectors (`text=…`, `label=…` is not supported by fill; use `[name=plz]`, `#id9`, `input[placeholder*=Ort]`)."""
    last = None
    for sel, value in (o["select"] or {}).items():
        try:
            await page.select_option(sel, label=str(value), timeout=3000)
        except Exception:
            try:
                await page.select_option(sel, value=str(value), timeout=3000)
            except Exception as e:
                notes.append(f"select {sel}: {str(e).splitlines()[0]}")
                continue
        last = sel
    for sel, value in (o["fill"] or {}).items():
        try:
            await page.fill(sel, str(value), timeout=3000)
            last = sel
        except Exception as e:
            notes.append(f"fill {sel}: {str(e).splitlines()[0]}")
    if o["submit"] and last:
        if isinstance(o["submit"], str):
            el = page.locator(o["submit"]).first
            await dom_click(el)
        else:
            await page.press(last, "Enter")
        await settle(page, o["wait"])
        notes.append("form submitted")


async def scroll(page, times, wait_ms, notes):
    for _ in range(times):
        await page.mouse.wheel(0, 20000)
        await page.wait_for_timeout(500)
        await settle(page, wait_ms)
    notes.append(f"scrolled to the bottom {times}x")


async def content_frames(page, max_frames, max_depth):
    """Visible child frames worth reading, depth first, skipping embeds such as videos, maps, captchas and trackers."""
    found = []

    async def walk(frame, depth):
        for child in frame.child_frames:
            if len(found) >= max_frames or depth > max_depth:
                return
            if child.is_detached() or not child.url.startswith("http") or IFRAME_SKIP_RE.search(child.url):
                continue
            try:
                if not await (await child.frame_element()).is_visible():
                    continue
            except Exception:
                continue
            found.append(child)
            await walk(child, depth + 1)
    await walk(page.main_frame, 1)
    return found


async def inline_iframes(frames, notes):
    """Replace each iframe element with its rendered body, so extraction sees embedded content as part of the parent page. Innermost first, so nested frames end up inside their parents."""
    done = 0
    for frame in reversed(frames):
        try:
            await frame.evaluate(MARK_HIDDEN)
            body = await frame.evaluate("() => document.body ? document.body.innerHTML : ''")
            handle = await frame.frame_element()
        except Exception:
            continue
        if not body.strip():
            continue
        await handle.evaluate("(el, args) => { const d = document.createElement('div'); d.setAttribute('data-inlined-iframe', args[0]); d.innerHTML = args[1]; el.replaceWith(d); }", [frame.url, body])
        done += 1
    if done:
        notes.append(f"inlined {done} iframe(s)")


async def click_rows(page, selector, max_rows, wait_ms, notes):
    """Open each element matching selector and record where it leads. For lists whose rows have no href and open the ad by click handler or window.open (b-ite on hockenheim.de). Returns [(row text, url)]."""
    rows = []
    loc = page.locator(selector)
    n = min(await loc.count(), max_rows)
    start = page.url
    for i in range(n):
        el = loc.nth(i)
        try:
            text = re.sub(r"\s+", " ", (await el.inner_text(timeout=1000)) or "").strip()
        except Exception:
            continue
        target = None
        try:
            async with page.context.expect_page(timeout=4000) as popup:
                await dom_click(el)
            new = await popup.value
            await new.wait_for_load_state("domcontentloaded", timeout=10000)
            target = new.url
            await new.close()
        except Exception:
            await settle(page, wait_ms)
            if page.url != start:
                target = page.url
                await page.goto(start, wait_until="domcontentloaded")
                await settle(page, wait_ms)
                loc = page.locator(selector)
        rows.append((text, target or "(click opened nothing)"))
    notes.append(f"click_rows: opened {len(rows)} of {await loc.count()} rows")
    return rows
