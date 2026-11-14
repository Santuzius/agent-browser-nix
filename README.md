# agent-browser-nix

Nix flake with browser tools for AI agents on Linux:

- **`web-renderer`:** reads the web pages plain fetchers cannot: JavaScript lists, load-more buttons, pagination, search forms, rows without links, iframes, web components, PDFs. It also finds an employer's careers page and reads its job list straight from the applicant tracking system (ATS) behind it. `web-renderer mcp` is a stdio MCP server for agents, `web-renderer URL [options]` the same on the command line.
- **`agent-browser`:** starts the agent Vivaldi, a visible Vivaldi with its own profile in `~/.config/agent-browser` and a DevTools port on 127.0.0.1:9333. Agents drive it through the Browser MCP extension (`@browsermcp/mcp`), and `web-renderer` reads a page there when its headless Chromium hits a bot check. The window has the class `agent-browser`; with plasma-manager the module adds a KWin rule that starts it minimized (`startMinimized`, on by default, overridable by a plasma-manager dump).

## Run

```bash
nix run github:Santuzius/agent-browser-nix -- https://example.com
nix run github:Santuzius/agent-browser-nix#agent-browser
```

## Home Manager module

```nix
{
  inputs.agent-browser-nix.url = "github:Santuzius/agent-browser-nix";

  # in the Home Manager modules:
  #   agent-browser-nix.homeManagerModules.default

  programs.agent-browser = {
    enable = true;
    extensions = [
      "bjfgambnhccakkhmkepdoekmckoijdlc" # Browser MCP (the default)
      "mdjildafknihdffpkfmmpnpoiajfjnjd" # Consent-O-Matic
    ];
  };
}
```

- `vivaldi`: the Vivaldi to start. The default `null` starts the `vivaldi` in `PATH`, so the one the system already has is used and none is installed in addition.
- `port`: DevTools port, default 9333.
- `extensions`: Chrome Web Store IDs, installed into the agent profile on its next start.
- `desktopEntry`: a desktop entry "Vivaldi (Agent)" with the Vivaldi logo and a blue robot as icon, default on.

Logins, extension settings and the Browser MCP pairing are profile data in `~/.config/agent-browser` and stay outside Nix. The DevTools port only listens on 127.0.0.1, but any local program can drive the agent Vivaldi and use its logins.

## MCP clients

Zed, in `~/.config/zed/settings.json`:

```json
"context_servers": {
  "web-renderer": { "command": "web-renderer", "args": ["mcp"] },
  "web-browser": {
    "command": "agent-browser",
    "args": ["exec", "npx", "--yes", "@browsermcp/mcp@2.4.0"]
  }
}
```

`agent-browser exec CMD` starts the agent Vivaldi if needed and then runs CMD, here the Browser MCP server. Profiles with `"enable_all_context_servers": false` also list the tools under `context_servers.<name>.tools`. For the `claude` CLI:

```bash
claude mcp add --scope user web-renderer -- web-renderer mcp
```

## Files

| Path | Content |
|---|---|
| `flake.nix`, `module.nix` | packages and Home Manager module |
| `agent-browser.nix`, `agent-browser.sh` | `agent-browser` |
| `web-renderer.nix` | `web-renderer`: the package below with nixpkgs' Python, Playwright, its Chromium and pypdf |
| `src/web_renderer/__main__.py` | entry point: `mcp` starts the MCP server, anything else the command line |
| `src/web_renderer/cli.py`, `mcp_server.py` | the two front ends: command-line options; MCP protocol and tool schemas |
| `src/web_renderer/render.py` | one URL end to end: headless first, agent Vivaldi after a bot check, then cleaning and parts |
| `src/web_renderer/headless.py` | loading a page in headless Chromium, PDFs, pagination, iframes |
| `src/web_renderer/agent_vivaldi.py` | the same in the agent Vivaldi over DevTools |
| `src/web_renderer/page_actions.py` | clicking, forms, consent banners, scrolling |
| `src/web_renderer/extraction.py` | Defuddle, Readability, whole page, and the choice between them |
| `src/web_renderer/markdown.py` | link tidying, grep, parts, output format |
| `src/web_renderer/options.py`, `environment.py` | option defaults; nixpkgs' Python when run from the checkout |
| `src/web_renderer/ats.py` | ATS adapters |
| `src/web_renderer/geizhals.py` | Geizhals category tree, product lists and product pages as structured data |
| `icon/` | icon of the agent Vivaldi and the robot badge it is made from |

To try a change without a rebuild, run the package from `src/` (`python3 -m web_renderer URL`); it then builds the same Python environment itself on first run. The icon is made in `icon/` with `rsvg-convert -w 100 -h 100 robot-badge.svg -o badge.png && magick <vivaldi>/share/icons/hicolor/256x256/apps/vivaldi.png badge.png -gravity southeast -geometry +6+6 -composite -strip agent-browser.png`.

It complements plain fetchers: a static reader sees no JavaScript, and Readability-based fetchers such as fetcher-mcp drop list items and never click anything.

## Why it exists

On 2026-10-07 a dozen agents searched municipal, utility and company careers pages in parallel. The protocol (1,382 tool calls) shows where the tooling failed:

- **About 300 calls** went to curl loops and throwaway parsers for the same ATS endpoints (Personio XML, Workday `cxs`, SmartRecruiters API …), with tenant names guessed and rediscovered by every agent.
- **About 85 calls and 15 hand-written Playwright scripts** handled numbered pagination, search forms (interamt) and table rows that open by click (b-ite on hockenheim.de). The old `render-page.py` could do none of these.
- **3 pages failed outright** with "Defuddle is not defined": on pages with an AMD loader (RequireJS, Wix, SAP UI5), the UMD bundle registered itself via `define()`.
- **35 calls** passed `--wait 5` meaning seconds and waited 5 ms.
- **47 times** the output was piped through `grep 'm/w/d'`; Bash-less agents could not use the script at all.

## Tools

| Tool           | Use                                                                                                    |
|----------------|--------------------------------------------------------------------------------------------------------|
| `render_page`  | One page as Markdown. Options: `pages`, `fill`/`select`/`submit`, `click_rows`, `grep`/`exclude`, `links`/`links_only`, `accept_cookies`, `mode`, `max_length`/`part`/`start`, `full_urls`, `browser` |
| `render_pages` | Up to 30 URLs with the same options, 4 at a time, one section per URL                                   |
| `link_urls`    | Full addresses behind link keys (`L7`) from earlier results; `render_page` accepts keys directly |
| `find_careers` | Home page → careers pages (best first) and the ATS behind them, with a ready `ats_jobs` call            |
| `ats_jobs`     | Every opening from the ATS: `title \| location \| date \| URL`, filtered by `query`/`location`/`exclude` |
| `geizhals_categories` | Geizhals category tree: areas, one area's tree, or matching categories with path, children (incl. preset filters) and siblings |
| `geizhals_list` | A Geizhals product list as a table, with sort options, pages, active filters and every filter as a short `xf` token; changed with `add`/`remove`, `sort`, `page`, `price_min`/`price_max` |
| `geizhals_product` | Offers (price, shop, country, shop rating, availability, delivery, shipping) and data sheet of one product |

On the command line (`web-renderer --help`) the same options apply, plus `--urls FILE --parallel N` for batches and `--python SCRIPT` to run an own Playwright script in the same environment.

## Renderer behaviour

- **Loading:** Images, fonts and media are blocked. Broken certificate chains are accepted, since the server only reads public pages. 404 and 410 stop with a clear message. 401, 403 and 5xx are extracted anyway, with a note, because guidecom and coveto serve their pages with a 401.
- **Load more:** Buttons that grow a list in place are clicked up to 10 times per frame. It stops as soon as a click adds no text, so a carousel no longer gets ten clicks. The default texts cover "Mehr laden", "Mehr anzeigen", "Weitere laden/anzeigen/Ergebnisse/Stellen", "Alle anzeigen", "Load more", "Show more"; others go into `expand_pattern` as plain texts separated by `|` (e.g. `Weitere Stellen|Mehr Treffer`), matched against the whole button text.
- **Pagination (`pages`):** "Weiter", "Next" and the like move to another page rather than growing the list, so they are handled here, not by load-more; other texts go into `next_pattern`. The next page is found via `data-page="n"`, `rel=next`, a next/weiter label, a "Weiter"/"›" text, or the button labelled n, in that order. Real links are navigated. With AJAX buttons, the renderer waits until the page text changes.
- **Rows without links (`click_rows`):** Every row is clicked. Popups and navigations are both caught.
- **Extraction:**
  - Defuddle, Readability and a whole-page mode run side by side. `auto` keeps whichever loses the least text. It takes the whole page when it holds at most 3,000 characters of text, when the extractor kept less than a third of it, when the extractor left a section empty that has text on the page, or when it dropped whole sections, heading included, that hold at least a quarter of the page text (Defuddle did this to the offer list of a Geizhals product page).
  - Sidebars (`aside`) stay unless they are mostly links; Fab keeps price and license in one. `keep_nav` also keeps navigation, header, footer and link-heavy sidebars.
  - Menus become compact tables, eight entries per row, holding their visible links, labelled buttons and settings (`◉ Standard`, `☐ Name`, `Sprache: Deutsch ▾`):
    - the top bar (the page's first `header`, else its first `nav`) comes first under `## Kopfleiste`;
    - every other `nav` outside header and footer (GitHub's profile tabs, Wikipedia's table of contents, breadcrumbs) stays where it stood, under its `aria-label` in bold;
    - a repeat of an earlier menu (same entries, or only links an earlier menu already had) is left out, and so are entries of closed hover menus; their parent link usually leads to the same pages;
    - `menus: off` leaves all menus out, e.g. when reading many search result pages; `menus: only` returns just the tables.
  - Links without text are named after their `aria-label` or `title`, or after the parent's `aria-label` when they are its only link; the bars of Geizhals' range-slider histograms become links like `[128GB bis 250GB (17 Produkte)](…)`.
  - With `grep`, the whole page is always searched.
  - If injecting the extractors fails, the page's plain text is returned.
- **iframes and web components:** Every visible iframe becomes a section of its own; web-component shadow DOM is flattened into the page.
- **PDF:** pypdf reads the text layer.
- **Times:** Milliseconds; values below 100 are read as seconds.
- **Output size:** Link texts are tidied, jump marks (`#offerlist`) and repeated targets become plain text, and targets over 100 characters become short keys (`[zum Angebot](L7)`). The server keeps the full addresses until it restarts: `render_page` opens a key directly, `link_urls` returns the address. On the command line, where nothing outlives the call, such targets lose their parameters (`https://geizhals.de/x.html?…`) or, if still long, their deeper path (`https://geizhals.de/redir/…`). `full_urls` keeps them whole. On a Geizhals product page with 32 offers this cut 57,000 characters to 23,000. Longer output is cut at paragraph breaks into parts; the footer says `[part 1/3 …; next: part=2]`.
- **Bot checks:** Headless Chromium does not pass Cloudflare (Geizhals answered "Nur einen Moment…" every time on 2026-11-13). With `browser: "auto"` (default) the renderer then reads the page again in the agent Vivaldi, starting it if needed; `browser: "agent"` goes there directly, e.g. for pages behind a login made in that profile. That path uses raw DevTools calls (`src/web_renderer/agent_vivaldi.py`), because Playwright's `connect_over_cdp` hangs on Vivaldi. It supports load-more buttons, `scroll`, `grep`, `links` and parts, but not `pages`, forms, `click_rows` or iframes.
- **Process:** One Chromium serves the whole process, with a fresh context per call and at most `RENDERER_CONCURRENCY` (default 4) pages at once. Requests are answered as they finish, so a slow page does not block the others.

## Geizhals (`src/web_renderer/geizhals.py`)

- **Categories:** from the JSON endpoint behind the breadcrumb menus (`POST /api/gh0/categories` with `{"params": {"m": "1", "lang": "de"}}`), which answers without a browser. Each node carries its id: area `m=1`, group `o=3`, category `cat=hdssd`, or a category with a preset filter such as `cat=hdssd xf=7525_M.2 (PCIe)`. Trees are cached for 15 minutes.
- **Lists:** rendered in headless Chromium. Cloudflare lays a check dialog over the page, but the content beneath is complete; the dialog is removed before any click. One JavaScript function reads products, filters, sort options and pagination. Filters come as tokens of the `xf` URL parameter (`ID_VALUE`, ranges `ID_MIN*MAX`), read off the option links, so `ab 480GB` under `Kapazität ab [7139]` is the token `7139_>=480GB`. `promode=true` is set by default, since the simple filter mode shows fewer filters.
- **Products:** headless Chromium gets Cloudflare's full check here, so product pages open in the agent Vivaldi. Shop links are session-bound redirects and are left out.
- **URL parameters:** `sort`, `pg` (page), `pagesize` (30, 100), `fs` (search), `bpmin`/`bpmax` (price), `t=v` (price incl. shipping), `hloc` (shop countries), `allvalues=true` (all values of a filter).

## ATS adapters (`src/web_renderer/ats.py`, stdlib only)

| System          | Recognised by                                     | Endpoint used                                                                    |
|-----------------|---------------------------------------------------|----------------------------------------------------------------------------------|
| Personio        | `{t}.jobs.personio.de/.com`                       | `GET /xml?language=de`                                                            |
| Workday         | `{t}.wd{N}.myworkdayjobs.com/{site}`              | `POST /wday/cxs/{t}/{site}/jobs` (20 per page, 8 in parallel, location facets)    |
| SmartRecruiters | `jobs.smartrecruiters.com/{Company}`              | `GET api.smartrecruiters.com/v1/companies/{Company}/postings?q=` (subsidiaries sit under the group, e.g. ETAS under `BoschGroup`) |
| Recruitee       | `{t}.recruitee.com`, widget `RTWidget({companies:[id]})` | `GET /api/offers/` or `api.recruitee.com/c/{id}/careers/offers`            |
| d.vinci         | `{t}.dvinci-hr.com`                               | `GET /jobPublication/list.json?lang=de`                                           |
| softgarden      | `{t}.softgarden.io`, "softgarden" in the HTML     | `GET /jobs.feed.json` (custom domains), else the links on `/de/vacancies`         |
| Lever           | `jobs.lever.co/{t}`                               | `GET api.lever.co/v0/postings/{t}?mode=json`                                      |
| Greenhouse      | `boards.greenhouse.io/{t}`                        | `GET boards-api.greenhouse.io/v1/boards/{t}/jobs`                                 |
| SuccessFactors  | `rmkcdn.successfactors.com`, `*.jobs.hr.cloud.sap` | `GET /search/?q=&startrow=N` (25 per page)                                       |
| Eightfold       | `eightfold.ai`, `/api/pcsx/`                      | `GET /api/pcsx/search?domain=&query=&location=&start=N`                           |
| dvvbw           | `{org}.bewerbung.dvvbw.de`, `*.ppa-duew.jobs`     | `GET /index?page=N`                                                               |
| mein-check-in   | `mein-check-in.de/{org}`                          | `GET /{org}/index`                                                                |
| guidecom        | `connect./jobs.guidecom.de/jobportal/{org}`       | `GET connect…/viewAusschreibungen.html` (`jobs.` answers 401)                     |

`find_careers` also names systems it recognises but has no adapter for, each with a pointer to what worked in the protocol:

- **Phenom:** `POST /widgets` with the page's `refineSearch` payload
- **b-ite:** `render_page` with `click_rows`
- **umantis:** the details are in an iframe; `render_page` reads them
- **rexx:** `stellenangebote.html?start=N`
- **Avature:** `SearchJobs/{q}/?folderOffset=N`
- **interamt:** detail pages need a browser session
- also helixjobs, onlyfy, coveto, hrworks, myjobboard, join.com and recruiting-portal.com

Tested against live endpoints on 2026-10-07: KACO (Personio), Hitachi (Workday, 23 Mannheim jobs out of 2,000 in about 20 s), Bosch (SmartRecruiters), Stadtwerke Esslingen (Recruitee widget found via `find_careers`), PSI (d.vinci), WVV (softgarden), Qwello (Lever), SMA and Fraunhofer (SuccessFactors), Infineon (Eightfold), Stadt Stuttgart (dvvbw, 454 jobs), Würzburg (mein-check-in) and Stadtwerke Pforzheim (guidecom). Renderer: Vermögen und Bau BW (AJAX pagination), Hockenheim (b-ite `click_rows`, 14 of 14), mein-check-in PDFs, bfe-institut.com (Wix, the former Defuddle crash).

## Caches

Defuddle and Readability are fetched from jsDelivr at pinned versions and cached in `~/.cache/web-renderer/`.

## Known gaps

- **interamt.de:** The search form has dynamic Wicket IDs and ignores URL parameters. Use `render_page` with `fill` on a stable selector (`input[placeholder*=…]`), or the authority's list `interamt.de/koop/app/trefferliste?partner={id}`.
- **umantis:** Pagination (`tc{N}=p{page}`) always returned page 1, even with a cookie jar.
- **persis:** Pagination runs via POST `page=N` with a session; not built in.
- **Bot protection:** Akamai blocked Tesla, Schneider Electric, SEW and HBK with a 403 in headless Chromium. Whether the agent Vivaldi gets through is untested; otherwise use the ATS API or search-engine snippets.
