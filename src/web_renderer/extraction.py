"""Turning a rendered frame into Markdown: Defuddle, Readability and a whole-page mode run in the page, and pick() chooses the result."""

import os
import re
import urllib.request
from pathlib import Path

JS_LIBS = {
    "defuddle": "https://cdn.jsdelivr.net/npm/defuddle@0.19.4/dist/index.full.js",
    "readability": "https://cdn.jsdelivr.net/npm/@mozilla/readability@0.6.0/Readability.js",
}
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "web-renderer"
AUTO_FULL_RATIO = 1.3
AUTO_FRAGMENT_RATIO = 3
AUTO_SMALL_PAGE = 3000
AUTO_DROPPED_SHARE = 0.25
NOISE_SELECTOR_RE = r"cookie|consent|gdpr|usercentrics|borlabs|klaro|cmp[-_]|onetrust|ccm-|skip-?link"


def js_lib(name):
    path = CACHE / (JS_LIBS[name].split("/npm/")[1].replace("/", "_"))
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(JS_LIBS[name], timeout=30) as r:
            path.write_bytes(r.read())
    return path.read_text()


def extractor_scripts():
    """JavaScript functions that install Defuddle and Readability as globals of the page. Evaluated via the DevTools protocol, so the page's Content-Security-Policy cannot block them.

    Defuddle ships as a UMD bundle: on pages with an AMD loader (SAP SuccessFactors/UI5, Wix, RequireJS) it registers itself through `define()` instead of setting `self.Defuddle`, and extraction then failed with "Defuddle is not defined". Local `define`/`module`/`exports` shadow the page's globals so the bundle always takes the plain-global branch."""
    return [
        "() => { var define, module, exports;\n" + js_lib("defuddle") + "\n}",
        "() => { var define, module, exports;\n" + js_lib("readability") + "\n window.Readability = Readability; }",
    ]


async def load_libs(frame):
    for script in extractor_scripts():
        await frame.evaluate(script)


MARK_HIDDEN = """() => {
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden' || el.getAttribute('aria-hidden') === 'true') el.setAttribute('data-rp-hidden', '');
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
}"""

# Web components (e.g. Jobcluster's <jcd-job-portal>) keep their content in a shadow root, which cloneNode, Defuddle and Readability never see. Copy each open shadow root into its host as ordinary children, innermost first.
FLATTEN_SHADOW = """() => {
  let n = 0;
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (!el.shadowRoot) continue;
      walk(el.shadowRoot);
      const d = document.createElement('div');
      d.setAttribute('data-shadow-root', el.tagName.toLowerCase());
      d.innerHTML = el.shadowRoot.innerHTML;
      el.appendChild(d);
      n++;
    }
  };
  walk(document);
  return n;
}"""

EXTRACT = """(args) => {
  const [mode, keepNav, noiseRe] = args;
  const url = location.href;
  const out = {};
  const md = (html) => Defuddle.createMarkdownContent(html, url);
  const re = new RegExp(noiseRe, 'i');
  // Every extractor gets a copy without hidden elements, media and consent banners; otherwise Readability happily returns a cookie dialog as the "article".
  const cleanClone = () => {
    const doc = document.cloneNode(true);
    doc.querySelectorAll('[data-rp-hidden],[role=dialog],[role=alertdialog],video,audio,picture source,canvas').forEach(e => e.remove());
    doc.querySelectorAll('[id],[class]').forEach(e => { if (re.test((e.id || '') + ' ' + (typeof e.className === 'string' ? e.className : ''))) e.remove(); });
    // Links without text vanish from Markdown. Name them after their aria-label or title, or after the parent's aria-label when they are its only link: the bars of Geizhals' range-slider histograms are such links ("2GB bis 120GB (keine Produkte)") and set the range filter.
    doc.querySelectorAll('a[href]').forEach(a => {
      if ((a.textContent || '').trim() || a.querySelector('img[alt]:not([alt=""])')) return;
      const p = a.parentElement;
      const label = a.getAttribute('aria-label') || a.title || (p && p.querySelectorAll('a').length === 1 && p.getAttribute('aria-label')) || '';
      if (label.trim()) a.appendChild(doc.createTextNode(label.trim()));
    });
    return doc;
  };
  if (mode === 'defuddle' || mode === 'auto') {
    try {
      const r = new Defuddle(cleanClone(), { url, markdown: true }).parse();
      out.defuddle = { content: r.content || '', title: r.title, author: r.author, published: r.published };
    } catch (e) { out.defuddle = { content: '', error: String(e) }; }
  }
  if (mode === 'readability' || mode === 'auto') {
    try {
      const r = new Readability(cleanClone()).parse();
      out.readability = { content: r && r.content ? md(r.content) : '', title: r && r.title };
    } catch (e) { out.readability = { content: '', error: String(e) }; }
  }
  if (mode === 'full' || mode === 'auto') {
    const doc = cleanClone();
    const drop = 'script,style,noscript,svg,template,iframe,link,meta' + (keepNav ? '' : ',nav,header,footer,[role=navigation],[role=banner],[role=contentinfo]');
    // Navs found by MENUS become placeholders that with_menus() turns into tables.
    if (!keepNav) doc.querySelectorAll('[data-rp-nav]').forEach(e => { const p = doc.createElement('p'); p.textContent = 'RPNAV' + e.getAttribute('data-rp-nav'); e.replaceWith(p); });
    doc.querySelectorAll(drop).forEach(e => e.remove());
    // Sidebars hold real data as often as noise: Fab puts price, license and publish date in an <aside>. Drop one only when it is mostly links (related products, tag clouds) or nearly empty.
    let asides = 0;
    if (!keepNav) {
      const linkDense = (e) => {
        const text = (e.textContent || '').replace(/\\s+/g, '').length;
        const linked = [...e.querySelectorAll('a')].reduce((n, a) => n + (a.textContent || '').replace(/\\s+/g, '').length, 0);
        return text < 30 || linked > 0.6 * text;
      };
      doc.querySelectorAll('aside,[role=complementary]').forEach(e => { if (linkDense(e)) { e.remove(); asides++; } });
    }
    try { out.full = { content: doc.body ? md(doc.body.innerHTML) : '', asides }; }
    catch (e) { out.full = { content: doc.body ? doc.body.innerText : '', error: String(e), asides }; }
  }
  if (mode === 'html') out.html = { content: document.documentElement.outerHTML };
  return out;
}"""

# Menus as compact tables, collected on the live page because visibility needs layout:
# - header: the page's top bar, the outermost header/banner outside main, article and footer that starts within the first 400 px, else the first such nav. heise marks every teaser module with <header>, so position decides, not the tag alone.
# - navs: every other outermost nav outside header and footer (GitHub's profile tabs, breadcrumbs, pagination). Each is marked data-rp-nav=N, which survives the clone, so full mode can put its table where the nav stood.
# Items are the visible links, buttons and settings (radio buttons and checkboxes as '◉ Standard' / '☐ Name', selects as 'Label: choice ▾') in page order, without repeats. Left out: entries of closed hover or click menus, and buttons whose only text is an aria-label or screen-reader-only (tagesschau's "Untermenü Inland einblenden", carousel arrows), since they only operate the menu. Links named only by an aria-label (a logo) stay.
MENUS = """() => {
  const outside = 'main, article, footer, aside, [role=main], [role=contentinfo], [role=complementary]';
  const atTop = (e) => { const r = e.getBoundingClientRect(); return r.height > 0 && r.top + scrollY < 400; };
  let roots = [...document.querySelectorAll('header, [role=banner]')].filter(e => !(e.parentElement && e.parentElement.closest('header, [role=banner], ' + outside)) && !e.closest('[data-rp-hidden]') && atTop(e));
  if (!roots.length) roots = [...document.querySelectorAll('nav, [role=navigation]')].filter(e => !e.closest(outside + ', [data-rp-hidden]') && atTop(e)).slice(0, 1);
  // Text a sighted reader sees: screen-reader-only spans (clipped or 1 px boxes) are skipped.
  const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 2 && r.height > 2 && s.clip === 'auto' && (s.clipPath === 'none' || !s.clipPath); };
  const visibleText = (el) => {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let t = '';
    for (let n = w.nextNode(); n; n = w.nextNode()) if (n.parentElement && shown(n.parentElement)) t += ' ' + n.textContent;
    return t;
  };
  const items = (containers) => {
    const out = [], seen = new Set();
    for (const root of containers) for (const el of root.querySelectorAll('a[href], button, [role=button], [role=menuitem], [role=tab], input[type=radio], input[type=checkbox], select')) {
      // Settings in a menu (Wikipedia's text size and colour): the option's label with its state, since the input itself is often styled away.
      if (el.tagName === 'INPUT' || el.tagName === 'SELECT') {
        const label = el.labels && el.labels[0];
        if (el.closest('[data-rp-hidden]') || !(label ? shown(label) : shown(el))) continue;
        const name = (label ? visibleText(label) : el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
        const text = el.tagName === 'SELECT' ? (name ? name + ': ' : '') + ((el.selectedOptions[0] || {}).text || '').trim() + ' ▾' : (el.type === 'radio' ? (el.checked ? '◉ ' : '○ ') : (el.checked ? '☑ ' : '☐ ')) + name;
        if (text.trim().length > 2 && !seen.has(text)) { seen.add(text); out.push([text, '']); }
        continue;
      }
      if (el.closest('[data-rp-hidden]') || (el.parentElement && el.parentElement.closest('a, button'))) continue;
      if (!shown(el) || (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true }))) continue;
      const href = el.tagName === 'A' && /^https?:/.test(el.href) ? el.href : '';
      const text = (visibleText(el).replace(/\\s+/g, ' ').trim() || (href && (el.getAttribute('aria-label') || el.title) || '')).replace(/\\s+/g, ' ').trim();
      if (!text || text.length > 60 || seen.has(text + '|' + href)) continue;
      seen.add(text + '|' + href);
      out.push([text, href]);
    }
    return out;
  };
  document.querySelectorAll('[data-rp-nav]').forEach(e => e.removeAttribute('data-rp-nav'));
  const navs = [];
  for (const nav of document.querySelectorAll('nav, [role=navigation]')) {
    if (roots.some(r => r.contains(nav)) || (nav.parentElement && nav.parentElement.closest('nav, [role=navigation]')) || nav.closest('footer, [role=contentinfo], [data-rp-hidden]')) continue;
    const found = items([nav]);
    if (!found.length) continue;
    const by = nav.getAttribute('aria-labelledby');
    const label = (nav.getAttribute('aria-label') || (by && document.getElementById(by) && document.getElementById(by).innerText) || '').replace(/\\s+/g, ' ').trim();
    nav.setAttribute('data-rp-nav', navs.length);
    navs.push({ label, items: found });
  }
  return { header: items(roots), navs };
}"""

LINKS = """() => [...document.querySelectorAll('a[href]')].map(a => [(a.innerText || a.title || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim(), a.href]).filter(([t, h]) => h.startsWith('http'))"""


MENU_COLUMNS = 8
MENU_MAX = 48
NAV_TOKEN_RE = re.compile(r"RPNAV(\d+)")


def menu_table(items, label, notes):
    """A menu as a Markdown table, MENU_COLUMNS cells per row: '| [Community](…) | [Neuigkeiten](…) | Anmelden |'. Buttons without a link stay plain text."""
    if not items:
        return ""
    if len(items) > MENU_MAX:
        notes.append(f"menu {label!r}: {len(items)} entries, kept the first {MENU_MAX}; keep_nav keeps the whole menu")
        items = items[:MENU_MAX]
    cells = [(f"[{t}]({h})" if h else t).replace("|", "\\|") for t, h in items]
    rows = [cells[i:i + MENU_COLUMNS] for i in range(0, len(cells), MENU_COLUMNS)]
    width = len(rows[0])
    lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
    lines.insert(1, "|" + "---|" * width)
    return "\n".join(lines)


def with_menus(body, menus, chosen, o, notes, header=True):
    """Put the top bar in front of the extracted text as a table under '## Kopfleiste', and turn the nav placeholders full mode left (RPNAV3) into tables labelled with the nav's aria-label. keep_nav in full mode leaves both in place as they are. menus='off' drops them, menus='only' returns the tables alone."""
    if o["menus"] == "off":
        return NAV_TOKEN_RE.sub("", body)
    if o["menus"] != "only" and o["keep_nav"] and chosen == "full":
        return body
    navs, seen, targets = menus["navs"], set(), set()

    # A nav that repeats an earlier one adds nothing: the same entries (heise's site index twice), or only links to targets an earlier menu already had (Geizhals' pagination above and below the list, the lower one with an extra "nächste" to page 2).
    def nav(m):
        n = int(m.group(1))
        if n >= len(navs):
            return ""
        texts = tuple(t for t, _ in navs[n]["items"])
        hrefs = {h for _, h in navs[n]["items"] if h}
        if texts in seen or (hrefs and hrefs <= targets):
            return ""
        seen.add(texts)
        targets.update(hrefs)
        table = menu_table(navs[n]["items"], navs[n]["label"], notes)
        return f"**{navs[n]['label']}:**\n\n{table}" if navs[n]["label"] else table
    if o["menus"] == "only":
        body = "\n\n".join(t for t in (nav(NAV_TOKEN_RE.match(f"RPNAV{n}")) for n in range(len(navs))) if t)
        notes.append("menus only: page text left out")
    else:
        body = NAV_TOKEN_RE.sub(nav, body)
    if not header:
        return body
    table = menu_table(menus["header"], "Kopfleiste", notes)
    return f"## Kopfleiste\n\n{table}\n\n{body}" if table else body


def text_weight(md):
    """Characters of real text, ignoring link targets and Markdown syntax, so a block of links does not outweigh a paragraph."""
    md = re.sub(r"\]\([^)]*\)", "]", md)
    return len(re.sub(r"[#*_>\[\]|`-]|\s+", "", md))


HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")


def heading_key(line):
    """Heading text without Markdown links and punctuation, so '## [7 Produkteigenschaften](#data)' matches across extractors."""
    return re.sub(r"\]\([^)]*\)|[^\w]+", "", line).lower()


def section_weights(md):
    """Text weight under each heading, keyed by heading_key; the first occurrence of a heading counts."""
    weights, key, size = {}, None, 0
    for line in md.split("\n"):
        m = HEADING_RE.match(line)
        if m:
            if key is not None:
                weights.setdefault(key, size)
            key, size = heading_key(m.group(1)), 0
        elif key is not None:
            size += text_weight(line)
    if key is not None:
        weights.setdefault(key, size)
    return weights


def emptied_sections(extracted, full):
    """Headings the extractor kept but left empty while the whole page has text under them. Readability did this to the spec table and document list of a Geizhals product page (2026-11-13), which the length ratio alone did not catch because the page also carries a long sidebar."""
    mine, whole = section_weights(extracted), section_weights(full)
    return [k for k, size in mine.items() if k and size < 20 and whole.get(k, 0) >= 60]


def dropped_sections(extracted, full):
    """Headings of the whole page that the extractor left out together with their text, if that text is at least AUTO_DROPPED_SHARE of the page. Defuddle dropped the offer list of a Geizhals product page this way, heading included, so the shop names were gone (2026-11-13)."""
    mine, whole = section_weights(extracted), section_weights(full)
    dropped = [k for k, size in whole.items() if k and k not in mine and size >= 60]
    total = sum(whole.values()) or 1
    return dropped if sum(whole[k] for k in dropped) >= AUTO_DROPPED_SHARE * total else []


def pick(res, mode, notes):
    """Choose the extraction result to return and say why."""
    if mode != "auto":
        chosen = mode
    else:
        weights = {k: text_weight(res[k]["content"]) for k in ("defuddle", "readability")}
        chosen = max(weights, key=weights.get)
        full = text_weight(res["full"]["content"])
        # If the whole page is barely longer than the best extraction, the page is nearly all content and the extractor only risks dropping items (Defuddle lost 3 of 21 jobs on a Jobcluster list). Much longer means the extractor cut real boilerplate, so keep its result, unless it emptied sections the page has text in.
        emptied = emptied_sections(res[chosen]["content"], res["full"]["content"])
        dropped = dropped_sections(res[chosen]["content"], res["full"]["content"])
        # Under a third is not boilerplate removal but a wrong guess at the "article": on the Geizhals search (2026-11-13) Readability returned only the price disclaimer below a 30-item product grid.
        fragment = full > AUTO_FRAGMENT_RATIO * weights[chosen]
        # A short page costs few tokens even whole, and extractors cut its sidebars, where shops keep price and license (Fab, 2026-11-13).
        small = full <= AUTO_SMALL_PAGE
        if full >= weights[chosen] and (weights[chosen] < 200 or small or full <= AUTO_FULL_RATIO * weights[chosen] or emptied or dropped or fragment):
            if emptied:
                notes.append(f"{chosen} left {len(emptied)} section(s) empty that have text on the page; using full")
            elif dropped:
                notes.append(f"{chosen} dropped {len(dropped)} section(s) holding at least {AUTO_DROPPED_SHARE:.0%} of the page text; using full")
            elif fragment:
                notes.append(f"{chosen} kept less than 1/{AUTO_FRAGMENT_RATIO} of the page text; using full")
            chosen = "full"
        notes.append("auto picked " + chosen + " (text chars: " + ", ".join(f"{k} {v}" for k, v in weights.items()) + f", full {full}); override with mode if this looks wrong")
    if chosen == "full" and res["full"].get("asides"):
        notes.append(f"dropped {res['full']['asides']} sidebar(s) that were mostly links; keep_nav keeps them")
    if res[chosen].get("error"):
        notes.append(f"{chosen} failed: {res[chosen]['error']}")
    return chosen, res[chosen]["content"].strip()


async def extract_frame(frame, o, notes, header=True):
    """Extract one frame as a document of its own: its own CSS decides what is hidden, its own shadow roots are flattened, and the extractor is chosen for it alone. Menus become tables (with_menus); header puts the top bar in front (off for further pages and iframes, where it would only repeat). Falls back to the frame's plain innerText if the extractors cannot even be injected."""
    try:
        if not o["no_details"]:
            await frame.evaluate("() => document.querySelectorAll('details:not([open])').forEach(d => d.open = true)")
        await frame.evaluate(MARK_HIDDEN)
        if not o["no_shadow"]:
            shadows = await frame.evaluate(FLATTEN_SHADOW)
            if shadows:
                notes.append(f"flattened {shadows} shadow root(s)")
        await load_libs(frame)
        menus = await frame.evaluate(MENUS)
        res = await frame.evaluate(EXTRACT, [o["mode"], o["keep_nav"], NOISE_SELECTOR_RE])
        chosen, body = pick(res, o["mode"], notes)
        body = with_menus(body, menus, chosen, o, notes, header)
    except Exception as e:
        notes.append(f"extractors failed ({str(e).splitlines()[0]}); returning the page's plain text")
        chosen, body = "innertext", (await frame.evaluate("() => document.body ? document.body.innerText : ''")).strip()
    links = await frame.evaluate(LINKS) if (o["links"] or o["links_only"]) else []
    return chosen, body, links
