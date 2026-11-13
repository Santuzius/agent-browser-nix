"""Geizhals (geizhals.de) as structured data: the category tree, product lists with every filter as a short xf token, and product pages with offers and specs.

Why a module of its own: product browsing is a frequent task, and the generic renderer leaves gaps there. The category menus in the breadcrumb only load on hover, every filter link repeats the whole address with all active filters (a list page came to 100,000 characters with all groups open), and headless Chromium gets Cloudflare's bot check on product pages. Here
- the category tree comes from the JSON endpoint behind those menus (POST /api/gh0/categories, answers without cookies or bot check);
- list pages are rendered headless (the bot-check dialog over them only blocks clicks, the content is complete) and read by one JavaScript function;
- product pages open in the agent Vivaldi, which passes the check;
- filters are written as tokens of the xf URL parameter (7136_ADATA, 7138_128000*250000), which geizhals_list takes back in add/remove.

URL scheme (observed 2026-11-13): ?m=N area, ?o=N group, ?cat=ID category; xf=ID_VALUE~ID_VALUE active filters (ID_MIN*MAX for ranges); sort=…, pg=N page, pagesize=30|100, fs=search text, hloc=de|at|… shop countries, t=v price including shipping; a3265324.html product.
"""

import asyncio
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import agent_vivaldi, ats
from .page_actions import settle

BASE = "https://geizhals.de/"
AREAS = {1: "Hardware", 2: "Software", 3: "Games", 4: "Video, Foto & TV", 5: "Telefon", 6: "Audio & HiFi", 7: "Filme", 8: "Haushalt", 9: "Sport & Freizeit", 10: "Drogerie", 11: "Auto & Motorrad", 12: "Spielzeug & Modellbau", 13: "Baumarkt & Garten", 14: "Büro & Schule"}
CACHE_SECONDS = 900  # the endpoint itself says max-age=900
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0 Safari/537.36"
_trees = {}


class GeizhalsError(Exception):
    pass


# ---------------------------------------------------------------- categories

def area_tree(m):
    """The category tree of area m from the JSON endpoint, cached for CACHE_SECONDS."""
    hit = _trees.get(m)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    status, _, text = ats.http(BASE + "api/gh0/categories", data={"params": {"m": str(m), "lang": "de"}}, headers={"Origin": BASE.rstrip("/")})
    if status != 200:
        raise GeizhalsError(f"category endpoint answered HTTP {status} for area m={m}")
    tree = json.loads(text)["categories"][0]
    _trees[m] = (time.time(), tree)
    return tree


def node_query(node_id):
    """{'cat': 'hdssd', 'xf': '7525_U.2/SFF-8639'} → 'cat=hdssd&xf=7525_U.2%2FSFF-8639'."""
    order = ["m", "o", "cat", "xf"]
    keys = sorted(node_id, key=lambda k: order.index(k) if k in order else len(order))
    return urllib.parse.urlencode([(k, node_id[k]) for k in keys])


def node_label(node):
    """'M.2 (PCIe) [cat=hdssd xf=7525_M.2 (PCIe)]': the id unencoded, as geizhals_list takes it."""
    nid = node.get("id") or {}
    return f"{node['title']} [" + " ".join(f"{k}={v}" for k, v in sorted(nid.items(), key=lambda kv: ["m", "o", "cat", "xf"].index(kv[0]) if kv[0] in ("m", "o", "cat", "xf") else 9)) + "]"


def walk(node, path):
    """Every node below node with its path (list of ancestor nodes, root first)."""
    for child in node.get("childs") or []:
        yield child, path
        yield from walk(child, path + [child])


def format_tree(node, depth, indent=0):
    lines = []
    for child in node.get("childs") or []:
        lines.append("  " * indent + "- " + node_label(child))
        if depth > 1:
            lines += format_tree(child, depth - 1, indent + 1)
    return lines


def categories(query=None, area=None, depth=2):
    """Text answer of geizhals_categories: the areas, one area's tree, or the categories matching query with path, children and siblings."""
    if not query and not area:
        lines = ["Areas (pass area=N for its tree, or query to search all areas):"]
        lines += [f"- {name} [m={m}]" for m, name in AREAS.items()]
        return "\n".join(lines)
    areas = [area] if area else list(AREAS)
    trees = {}
    errors = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for m, result in zip(areas, pool.map(lambda m: _safe(area_tree, m), areas)):
            if isinstance(result, Exception):
                errors.append(f"m={m}: {result}")
            else:
                trees[m] = result
    out = []
    if not query:
        tree = trees.get(area)
        if tree is None:
            raise GeizhalsError("; ".join(errors))
        out.append(f"# {node_label(tree)}, {depth} level(s); URL: {BASE}?{node_query(tree['id'])}")
        out += format_tree(tree, depth)
        return "\n".join(out)
    rx = re.compile(query, re.I)
    hits = []
    for m, tree in trees.items():
        for node, path in walk(tree, [tree]):
            nid = node.get("id") or {}
            if rx.search(node["title"]) or ("xf" not in nid and rx.fullmatch(str(nid.get("cat", "")))):
                hits.append((node, path))
    if not hits:
        out.append(f"No category matches {query!r}.")
    for node, path in hits[:15]:
        parent = path[-1]
        out.append(f"## {node['title']}")
        out.append("Path: " + " › ".join(node_label(p) for p in path + [node]))
        out.append(f"URL: {BASE}?{node_query(node.get('id') or {})}")
        if node.get("childs"):
            out.append("Children: " + " · ".join(node_label(c) for c in node["childs"]))
        siblings = [c for c in parent.get("childs") or [] if c is not node]
        if siblings:
            out.append("Siblings: " + " · ".join(node_label(c) for c in siblings))
        out.append("")
    if len(hits) > 15:
        out.append(f"{len(hits) - 15} more match(es); narrow the query.")
    out += [f"Note: area {e}" for e in errors]
    return "\n".join(out).rstrip()


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return e


# ---------------------------------------------------------------- URLs and xf tokens

def product_url(ref):
    """'a3265324', '3265324' or a product URL → URL."""
    ref = ref.strip()
    if re.fullmatch(r"a?\d+", ref):
        return f"{BASE}a{ref.lstrip('a')}.html"
    return urllib.parse.urljoin(BASE, ref)


def split_url(url):
    """(path, ordered params without xf, xf tokens) of a Geizhals URL."""
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, url))
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    xf = []
    rest = []
    for k, v in params:
        if k == "xf":
            xf += [t for t in v.split("~") if t]
        else:
            rest.append((k, v))
    return parts.path, rest, xf


def build_url(path, params, xf):
    query = [p for p in params]
    if xf:
        query.insert(1 if query and query[0][0] == "cat" else 0, ("xf", "~".join(xf)))
    return urllib.parse.urlunsplit(("https", "geizhals.de", path or "/", urllib.parse.urlencode(query), ""))


def list_url(url=None, cat=None, add=(), remove=(), sort=None, page=None, pagesize=None, search=None, params=None):
    if url and re.fullmatch(r"v\d+", url.strip()):
        url = f"{BASE}{url.strip()}.html"  # variant list, as shown after a product in geizhals_list
    path, rest, xf = split_url(url or BASE + (f"?cat={urllib.parse.quote(cat)}" if cat else ""))
    if cat and url:
        rest = [(k, v) for k, v in rest if k != "cat"] + [("cat", cat)]
    xf = [t for t in xf if t not in set(remove)] + [t for t in add if t not in xf]
    updates = {"sort": sort, "pg": page, "pagesize": pagesize, "fs": search, **(params or {})}
    # The advanced filter mode lists every filter; without promode the site falls back to the short list.
    if not any(k == "promode" for k, _ in rest) and "promode" not in updates:
        updates["promode"] = "true"
    for key, value in updates.items():
        if value is None:
            continue
        rest = [(k, v) for k, v in rest if k != key]
        if value != "":
            rest.append((key, str(value)))
    if path in ("", "/") and not any(k in ("cat", "fs", "m", "o") for k, _ in rest):
        raise GeizhalsError("give a list URL, cat or search")
    # cat first, as on the site
    rest.sort(key=lambda kv: kv[0] != "cat")
    return build_url(path, rest, xf)


def change(href, current):
    """What following href changes against the current list URL: (added xf tokens, removed xf tokens, other changed params)."""
    _, cur_rest, cur_xf = split_url(current)
    _, new_rest, new_xf = split_url(href)
    added = [t for t in new_xf if t not in cur_xf]
    removed = [t for t in cur_xf if t not in new_xf]
    cur, new = dict(cur_rest), dict(new_rest)
    other = {k: v for k, v in new.items() if cur.get(k) != v}
    other.update({k: "" for k in cur if k not in new})
    return added, removed, other


# ---------------------------------------------------------------- page access

async def evaluate(chromium, url, js, arg, wait=8000, browser="auto"):
    """Open url, run the async JavaScript function js(arg) in it and return (value, final URL, notes). Headless first; on a Cloudflare check page (or with browser='agent') in the agent Vivaldi."""
    notes = []
    if browser != "agent":
        ctx = await chromium.new_context(locale="de-DE", viewport={"width": 1566, "height": 1400}, user_agent=UA)
        try:
            async def skip_media(r):
                await (r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
            await ctx.route("**/*", skip_media)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await settle(page, wait)
            if not agent_vivaldi.BOT_TITLE_RE.search(await page.title()):
                return await page.evaluate(js, arg), page.url, notes
            notes.append("headless Chromium got Cloudflare's check; read in the agent Vivaldi instead")
        finally:
            await ctx.close()
    async with agent_vivaldi.websockets.connect(await asyncio.to_thread(agent_vivaldi.ws_url), max_size=None, open_timeout=15) as ws:
        tab = agent_vivaldi.Tab(ws)
        try:
            await tab.open()
            await tab.send("Page.navigate", {"url": url})
            # Cloudflare's check reloads the page once it passes; wait for the real title.
            for _ in range(60):
                await tab.settle(1500)
                try:
                    title = await tab.eval("() => document.title", timeout=5)
                except Exception:
                    continue
                if not agent_vivaldi.BOT_TITLE_RE.search(title or "") and title:
                    break
            await tab.settle(wait)
            value = await tab.eval(js, arg, timeout=90)
            final = await tab.eval("() => location.href")
            return value, final, notes
        finally:
            await tab.close()


# Shared helpers inside the page. clean(): visible text in one line; the bot-check dialog over list pages only blocks clicks, so it is removed before any click.
JS_HELPERS = r"""
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const clean = (s) => (s || '').replace(/​/g, '').replace(/\s+/g, ' ').trim();
  const text = (el, sel) => { const e = sel ? el.querySelector(sel) : el; return e ? clean(e.innerText || e.textContent) : ''; };
  document.getElementById('dialog-turnstile')?.remove();
"""

LIST_JS = r"""async (opts) => {
""" + JS_HELPERS + r"""
  if (opts.allFilters) {
    const btn = [...document.querySelectorAll('button, a')].find(b => /^alle filter ausklappen$/i.test(clean(b.innerText)));
    if (btn) {
      const before = document.querySelectorAll('.filter-element').length;
      btn.click();
      for (let i = 0; i < 40 && document.querySelectorAll('.filter-element').length <= before; i++) await sleep(250);
      await sleep(500);
    }
  }
  // The sort menu renders its options only when its summary is clicked.
  document.querySelectorAll('details').forEach(d => { if (!d.open) d.querySelector('summary')?.click(); });
  await sleep(600);

  const out = { heading: text(document, 'h1'), breadcrumb: [], products: [], groups: [], active: [], sorts: [], pages: null, pagesizes: [], price: [] };
  document.querySelectorAll('nav[aria-label=breadcrumbs] li a').forEach(a => out.breadcrumb.push([clean(a.innerText), a.href]));

  // Products: gallery view (headless default) and list view (the user's setting in the agent Vivaldi) share the class suffixes.
  const items = document.querySelectorAll('[class*="view__item"]');
  items.forEach(item => {
    const name = item.querySelector('[class*="view__name-link"], [class*="view__name"] a');
    if (!name) return;
    const specs = [...item.querySelectorAll('.fcols-data dt')].map(dt => [clean(dt.innerText), clean(dt.nextElementSibling && dt.nextElementSibling.innerText)]);
    const rating = item.querySelector('.stars-rating');
    const avail = item.querySelector('.availability');
    const variant = item.querySelector('[class*="view__variant"]');
    out.products.push({
      name: clean(name.innerText), href: name.href,
      mpn: text(item, '[class*="view__mpn"]'),
      rating: rating ? (rating.style.getPropertyValue('--stars-rating') || '').trim() : '',
      ratings: (text(item, '.stars-rating-label-bottom').match(/\d+/) || [''])[0],
      price: [text(item, '[class*="view__price-before"]'), text(item, '.price')].filter(Boolean).join(' '),
      ppu: text(item, '[class*="view__ppu"]').replace(/^\(|\)$/g, ''),
      availability: avail ? avail.getAttribute('aria-label') || '' : '',
      offers: (text(item, '[class*="view__offercount"]').match(/\d+/) || [''])[0],
      variants: variant ? [clean(variant.innerText), variant.href] : null,
      specs,
    });
  });

  // Filters: rows (groups) hold elements; an element holds tags (options), slider buckets or a "weitere" link.
  document.querySelectorAll('.filter-row').forEach(row => {
    const label = row.querySelector('.filter-row-label');
    const group = { title: label ? text(label, 'span') || text(label) : '', open: !!row.querySelector('.filter-element'), elements: [] };
    row.querySelectorAll('.filter-element').forEach(el => {
      const e = { id: el.dataset.filterId, type: el.dataset.filterType, title: text(el, '.filter-element__title-label'), options: [], buckets: [], more: null, range: null };
      el.querySelectorAll('a.filter-tag-item').forEach(a => {
        const count = text(a, '.filter-tag-count');
        e.options.push({ label: clean(text(a, '.filter-tag-label').replace(count, '')), count: count.replace(/[()]/g, ''), href: a.href, active: a.classList.contains('active') });
      });
      // Histogram bars are links (a.bucket) when the range holds products, plain divs otherwise.
      el.querySelectorAll('a.bucket[href]').forEach(b => e.buckets.push({ label: b.getAttribute('aria-label') || '', href: b.href }));
      const more = el.querySelector('a.filter-tags-expand-button');
      if (more) e.more = more.href;
      const pips = [...el.querySelectorAll('.rsPipVal')].map(p => clean(p.innerText));
      if (pips.length) e.range = [pips[0], pips[pips.length - 1]];
      group.elements.push(e);
    });
    out.groups.push(group);
  });

  document.querySelectorAll('.active-filters .selected-filter-group').forEach(g => {
    const name = text(g, 'strong').replace(/:$/, '');
    g.querySelectorAll('a.filter-tag-item').forEach(a => out.active.push({ name, label: text(a, '.filter-tag-label'), href: a.href }));
  });
  const reset = document.querySelector('.active-filters a.reset-filters');
  out.reset = reset ? reset.href : null;

  // Price slider in the sidebar: buckets are links like the other sliders.
  document.querySelectorAll('.sidebar a.bucket[href]').forEach(b => out.price.push({ label: b.getAttribute('aria-label') || '', href: b.href }));

  document.querySelectorAll('details').forEach(d => {
    if (!/sortierung/i.test(text(d, 'summary'))) return;
    out.sortCurrent = text(d, 'summary').replace(/^sortierung:\s*/i, '');
    d.querySelectorAll('a[href]').forEach(a => out.sorts.push([clean(a.innerText), a.href, a.getAttribute('aria-current') === 'true' || a.classList.contains('active')]));
  });
  const pag = document.querySelector('nav.pagination, nav[aria-label=Pagination]');
  if (pag) {
    const nums = [...pag.querySelectorAll('a.pagination__page')].map(a => parseInt(clean(a.innerText).replace(/\D/g, ''), 10)).filter(n => !isNaN(n));
    const cur = pag.querySelector('[aria-current=page]');
    out.pages = { current: cur ? parseInt(clean(cur.innerText).replace(/\D/g, ''), 10) : 1, last: nums.length ? Math.max(...nums) : 1 };
  }
  document.querySelectorAll('#per-page-select option').forEach(o => out.pagesizes.push([o.value, o.selected]));
  out.count = (text(document, 'h1').match(/\(([\d.]+) Produkte?\)/) || ['', ''])[1];
  return out;
}"""

PRODUCT_JS = r"""async (opts) => {
""" + JS_HELPERS + r"""
  document.querySelectorAll('.specs-grid').forEach(d => d.classList.add('no-limit'));
  const out = { name: text(document, 'h1'), specs: [], offers: [], variants: [], breadcrumb: [] };
  document.querySelectorAll('nav[aria-label=breadcrumbs] li a').forEach(a => out.breadcrumb.push([clean(a.innerText), a.href]));
  const head = document.querySelector('h1') ? document.querySelector('h1').closest('div, section') : null;
  out.mpn = '';
  document.querySelectorAll('.specs-grid__item').forEach(i => out.specs.push([text(i, 'dt'), text(i, 'dd')]));
  const r = document.querySelector('.stars-rating');
  out.rating = r ? (r.style.getPropertyValue('--stars-rating') || '').trim() : '';
  // Variants: links to the sibling products (other capacities, colours).
  document.querySelectorAll('a[href*="-a"][href$=".html"], a[href*="-a"][href*=".html?"]').forEach(a => {
    if (a.hostname !== location.hostname || a.closest('.offer, .toplist, nav, header, footer, .ratings-item, .loc-selector, .breadcrumbs')) return;
    const t = clean((a.title || a.innerText).replace(/Bewertung:[\s\S]*$/, ''));
    if (t && t.length < 80 && /-a\d+\.html/.test(a.href) && !out.variants.some(v => v[1] === a.href)) out.variants.push([t, a.href]);
  });
  const all = document.querySelector('a[href*="-v"][href$=".html"]');
  out.variantList = all ? [clean(all.innerText), all.href] : null;
  document.querySelectorAll('.offer').forEach(o => {
    const merchant = o.querySelector('.offer__merchant');
    const link = merchant ? merchant.querySelector('[data-merchant-name]') : null;
    const name = link ? link.dataset.merchantName : (merchant ? text(merchant, '.merchant__logo-caption') || text(merchant).split(' ')[0] : '');
    const flag = merchant ? merchant.querySelector('img[alt]') : null;
    const rating = o.querySelector('.offer__merchant-rating');
    const rtext = rating ? text(rating) : '';
    const delivery = o.querySelector('.offer__delivery');
    const time = text(o, '.offer__delivery-time');
    const avail = (o.className.match(/offer--(available|shortly|unavailable)/) || ['', ''])[1];
    out.offers.push({
      price: text(o, '.gh_price') ? [text(o, '.offer__price').match(/^ab\b/i) ? 'ab' : '', text(o, '.gh_price')].filter(Boolean).join(' ') : '',
      merchant: name, country: flag ? flag.alt : '',
      rating: (rtext.match(/Bewertung: ([\d.,]+) von 5/) || ['', ''])[1], ratings: (rtext.match(/(\d+) Bewertung/) || ['', ''])[1],
      availability: avail, time,
      shipping: delivery ? clean(text(delivery).replace(time, '')) : '',
      note: merchant ? (text(merchant).match(/Hinweis: [^.]*?(?=Infos|AGB|$)/) || [''])[0].trim() : '',
    });
  });
  return out;
}"""


# ---------------------------------------------------------------- formatting

def cell(s):
    return (s or "").replace("|", "\\|").strip()


def token_text(token, fid):
    """'7136_ADATA' shown under filter 7136 as 'ADATA'."""
    return token[len(fid) + 1:] if fid and token.startswith(fid + "_") else token


def option_text(href, current, fid, label):
    """How an option link changes the list, as short as possible: nothing when it toggles exactly 'ID_label', '{value}' when its token value differs from the label ('ab 480GB {>=480GB}'), and any further change (other filters dropped, parameters) after it. Replacing the element's own active value is implied and not shown."""
    added, removed, other = change(href, current)
    own = [t for t in added + removed if fid and t.startswith(fid + "_")]
    bits = []
    values = [token_text(t, fid) for t in own if t in added] or [token_text(t, fid) for t in own if t in removed]
    if values and values[0] != label:
        bits.append("{" + values[0] + "}")
    bits += [f"+{t}" for t in added if t not in own]
    bits += [f"−{t}" for t in removed if t not in own and not (fid and t.startswith(fid + "_"))]
    bits += [f"{k}={v}" if v else f"−{k}" for k, v in other.items() if k != "pg"]
    return " ".join(bits)


def format_list(data, url, all_filters, show_filters, max_products):
    out = []
    path, _, xf = split_url(url)
    out.append(f"# {data['heading'] or 'Geizhals list'}")
    out.append(f"URL: {url}")
    if data["breadcrumb"]:
        crumbs = []
        for t, h in data["breadcrumb"]:
            q = urllib.parse.urlsplit(h).query
            crumbs.append(f"{t} [{q}]" if q else t)
        cat = dict(split_url(url)[1]).get("cat")
        if cat:
            crumbs.append(f"{re.sub(r' [(][0-9.]+ Produkte?[)]$', '', data['heading'].split('»')[-1]).strip()} [cat={cat}]")
        out.append("Path: " + " › ".join(crumbs) + " (siblings and children: geizhals_categories)")
    pages = data.get("pages") or {"current": 1, "last": 1}
    sizes = "|".join(v for v, _ in data["pagesizes"]) or "30|100"
    out.append(f"Page {pages['current']} of {pages['last']} (page=N) · pagesize {next((v for v, s in data['pagesizes'] if s), '?')} ({sizes})" + (f" · {data['count']} products" if data.get("count") else ""))
    if data.get("sorts"):
        opts = []
        for label, href, _ in data["sorts"]:
            opts.append(f"{label} [{dict(split_url(href)[1]).get('sort', '')}]")
        out.append(f"Sort: {data.get('sortCurrent', '?')} [{dict(split_url(url)[1]).get('sort', '')}] · options (sort=…, [] = default): " + " · ".join(opts))
    elif data.get("sortCurrent"):
        out.append(f"Sort: {data['sortCurrent']} [{dict(split_url(url)[1]).get('sort', '')}]")
    if data["active"]:
        def removal(href):
            _, removed, other = change(href, url)
            parts = ["remove=" + "~".join(removed)] if removed else []
            return " ".join(parts + [f"params {k}=\"{v}\"" for k, v in other.items()])
        out.append("Active filters: " + " · ".join(f"{a['name']}: {a['label']} [{removal(a['href'])}]" for a in data["active"]))
    out.append("")

    products = data["products"][:max_products]
    if not products:
        out.append("No products on this page.")
    else:
        spec_names = []
        for p in products:
            for k, _ in p["specs"]:
                if k not in spec_names:
                    spec_names.append(k)
        out.append("## Products ([a…] → geizhals_product, [v…] variant group → geizhals_list url)")
        out.append("")
        header = ["#", "Product", "Price", "Per unit", "Rating", "Offers"] + [cell(n) for n in spec_names]
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "---|" * len(header))
        start = (pages["current"] - 1) * int(next((v for v, s in data["pagesizes"] if s), "30") or 30)
        for i, p in enumerate(products, start + 1):
            # A product (…-a123.html, for geizhals_product) or a variant group (…-v123.html, for geizhals_list).
            pid = re.search(r"-([av]\d+)\.html", p["href"])
            name = f"{cell(p['name'])} [{pid.group(1)}]" if pid else cell(p["name"])
            if p["mpn"]:
                name += f" · {cell(p['mpn'])}"
            if p["variants"]:
                vid = re.search(r"-v(\d+)\.html", p["variants"][1])
                n = re.search(r"\d+", p["variants"][0])
                name += f" · {n.group(0) if n else 'all'} variants" + (f" [v{vid.group(1)}]" if vid else "")
            price = cell(p["price"]) + (f" ({p['availability']})" if p["availability"] else "")
            rating = f"{float(p['rating']):.1f} ({p['ratings']})" if p["rating"] else "–"
            specs = dict(p["specs"])
            row = [str(i), name, price, cell(p["ppu"]), rating, p["offers"]] + [cell(specs.get(n, "")) for n in spec_names]
            out.append("| " + " | ".join(row) + " |")
        if len(data["products"]) > max_products:
            out.append(f"\n{len(data['products']) - max_products} more product(s) on this page; raise max_products.")

    if show_filters:
        out.append("")
        out.append("## Filters (token = ID_VALUE, VALUE is the label unless given in {}; add=[token] / remove=[token]; ✓ active)")
        if data["price"]:
            out.append("- Price buckets: " + " · ".join(f"{cell(b['label'])} {option_text(b['href'], url, '', '')}" for b in data["price"]))
        closed = []
        for g in data["groups"]:
            if not g["open"]:
                closed.append(g["title"])
                continue
            out.append(f"\n**{g['title']}**")
            for e in g["elements"]:
                line = f"- {e['title']} [{e['id']}]: "
                parts = []
                for o in e["options"]:
                    note = option_text(o["href"], url, e["id"], o["label"])
                    parts.append(("✓" if o["active"] else "") + f"{o['label']}" + (f" ({o['count']})" if o["count"] else "") + (f" {note}" if note else ""))
                if e["range"]:
                    parts.append(f"range {e['range'][0]}–{e['range'][1]}, token {e['id']}_MIN*MAX in the unit of the bars")
                if e["buckets"]:
                    parts.append("bars: " + " · ".join(f"{cell(re.sub(r' Produkte?\)$', ')', b['label']))} {option_text(b['href'], url, e['id'], '')}" for b in e["buckets"]))
                if e["more"]:
                    _, _, other = change(e["more"], url)
                    parts.append("more values: params=" + json.dumps(other, ensure_ascii=False))
                out.append(line + (" · ".join(parts) if parts else "(no options shown)"))
        if closed:
            out.append("\nClosed groups (all_filters=true opens them): " + " · ".join(closed))
    return "\n".join(out)


def format_product(data, url, max_offers):
    out = [f"# {data['name']}", f"URL: {url}"]
    if data["breadcrumb"]:
        out.append("Path: " + " › ".join(f"{t} [{urllib.parse.urlsplit(h).query}]" if urllib.parse.urlsplit(h).query else t for t, h in data["breadcrumb"]))
    if data["rating"]:
        out.append(f"Rating: {float(data['rating']):.1f} of 5")
    if data.get("variantList"):
        out.append(f"Variants: {data['variantList'][0]} ({data['variantList'][1]})")
    if data["variants"]:
        out.append("Variant products: " + " · ".join(f"{cell(t)} [a{re.search(r'-a(\d+)', h).group(1)}]" for t, h in data["variants"][:20]))
    offers = data["offers"]
    out.append("")
    out.append(f"## Offers ({len(offers)}; cheapest first as on the page; shop links are session-bound redirects and left out)")
    out.append("")
    out.append("| # | Price | Shop | Country | Shop rating | Availability | Delivery | Shipping, payment |")
    out.append("|---|---|---|---|---|---|---|---|")
    avail = {"available": "in stock", "shortly": "shortly", "unavailable": "not in stock"}
    for i, o in enumerate(offers[:max_offers], 1):
        shop = cell(o["merchant"]) + (f" ({cell(o['note'])})" if o["note"] else "")
        rating = f"{o['rating']} ({o['ratings']})" if o["rating"] else "–"
        ship = cell(o["shipping"])
        out.append(f"| {i} | {cell(o['price'])} | {shop} | {cell(o['country'])} | {rating} | {avail.get(o['availability'], o['availability'] or '?')} | {cell(o['time'])} | {ship[:160] + ('…' if len(ship) > 160 else '')} |")
    if len(offers) > max_offers:
        out.append(f"\n{len(offers) - max_offers} more offer(s); raise max_offers.")
    if data["specs"]:
        out.append("")
        out.append("## Specs")
        out.append("")
        out += [f"- **{cell(k)}:** {cell(v)}" for k, v in data["specs"]]
    return "\n".join(out)


# ---------------------------------------------------------------- tools

async def geizhals_list(chromium, a):
    params = dict(a.get("params") or {})
    if a.get("price_min") is not None:
        params["bpmin"] = a["price_min"]
    if a.get("price_max") is not None:
        params["bpmax"] = a["price_max"]
    if a.get("incl_shipping") is not None:
        params["t"] = "v" if a["incl_shipping"] else ""
    url = list_url(a.get("url"), a.get("cat"), a.get("add") or (), a.get("remove") or (), a.get("sort"), a.get("page"), a.get("pagesize"), a.get("search"), params)
    data, final, notes = await evaluate(chromium, url, LIST_JS, {"allFilters": bool(a.get("all_filters"))}, browser=a.get("browser", "auto"))
    text = format_list(data, final or url, a.get("all_filters"), a.get("filters", True), a.get("max_products") or 100)
    return "\n".join([*(f"Note: {n}" for n in notes), text])


async def geizhals_product(chromium, a):
    url = product_url(a["product"])
    params = []
    if a.get("incl_shipping"):
        params.append(("t", "v"))
    params += [("hloc", c.strip()) for c in (a.get("countries") or "").split(",") if c.strip()]
    if params:
        path, rest, xf = split_url(url)
        rest = [(k, v) for k, v in rest if k not in dict(params)] + params
        url = build_url(path, rest, xf)
    data, final, notes = await evaluate(chromium, url, PRODUCT_JS, {}, browser="agent")
    return "\n".join([*(f"Note: {n}" for n in notes), format_product(data, final or url, a.get("max_offers") or 30)])
