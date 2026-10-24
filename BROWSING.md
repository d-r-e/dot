# Browsing and accessibility refs

## Runtime: official Playwright MCP

`mcp_browser.py` owns a private stdio MCP session per conversation, using the
official Python MCP SDK. `mcp_browser.mjs` embeds pinned `@playwright/mcp@0.0.83`
over the official SDK's in-memory transport and exports a policy-enforcing stdio
gateway. It is not a remote MCP listener and never attaches to a personal profile.
The LLM/tool loop remains in dot rather than llama.cpp's web UI.

The native catalog has 72 tools, discovered without launching Chromium. Default
exposure is navigation/back, snapshot, click, type, hover, wait and close.
`DOT_MCP_ALL_TOOLS=1` exposes every schema for experiments, marking unavailable
ones as disabled; **it does not change execution permissions**. Requests for code,
eval, files, storage/cookies, routing changes, arbitrary selectors or page-defined
WebMCP tools cannot cross the gateway. Automatic snapshot artifact links are
followed by an explicit MCP snapshot call, not filesystem reads.

`browser_tool.py` still provides URL/DNS policy and the original direct browser
wrapper for regression tests; production `Toolbox` uses `MCPBrowserSession`.

## Why replace web_search?

Live validation on 2026-10-23 used the existing implementation with an AEMET
weather query and a scoped documentation query. Both failed: DuckDuckGo returned
an HTTP 202 antibot challenge on each attempt; Brave returned 429, including its
retry. Neither supplied usable results. The curl scrapers and search cache have
been removed rather than presented as reliable search.

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `browser_navigate` (`browse` / `navigate` aliases) | `url` | Navigate through MCP and return an accessibility snapshot |
| `browser_click` | `target` | Click the exact ARIA ref and return a fresh snapshot |
| `browser_type` | `target`, `text` | Fill a field; no Enter or automatic submission |
| `browser_navigate_back` / `browser_hover` / `browser_wait_for` | native MCP parameters | Back navigation, exact-ref hover, bounded wait |
| `browser_snapshot` | none | Return the current view again with fresh refs |
| `browser_close` | none | Close the browser; only on an explicit user request |
| `weather` | `place` | Open-Meteo current conditions and three-day forecast |
| `current_time` | none | Local clock in Europe/Madrid, including UTC offset |

Every browser action is a blocking tool: any answer the model invents after
emitting the call is discarded until the real result is available. The default
tool-round budget is six. All tools can be disabled with `DOT_TOOLS=0`, or just
browser tools with `DOT_BROWSE=0`. The former `DOT_SEARCH` switch remains a
fallback for installations that have not migrated; old search limit/TTL settings
are no longer used.

## The Playwright MCP pattern, not a CSS inventory

The implementation follows the accessibility-first design of
[Playwright MCP](https://github.com/microsoft/playwright-mcp):

- [`page.aria_snapshot(mode="ai")`](https://playwright.dev/python/docs/api/class-page#page-aria-snapshot)
  supplies computed roles, accessible names, hierarchy and `[ref=eN]` references.
- Native MCP `target` resolves refs to actual Playwright locators. The gateway
  validates refs before MCP execution; the pinned Node implementation uses
  Playwright's built-in `aria-ref` engine (Python exposes `get_by_ref`).
- `.click()` and `.fill()` act on those locators. There are no model-supplied CSS
  selectors, JavaScript expressions, DOM data attributes, or screenshot coordinates.
- References must appear in the most recently returned snapshot. Playwright may
  keep a ref stable for the same live element; it must never be guessed or reused
  from a different page. Only refs in the returned, untruncated portion are allowed.

For example, a snapshot may contain:

```yaml
- main [ref=e2]:
  - textbox "Consulta" [ref=e6]
  - button "Buscar" [ref=e7]
```

The model calls `browser_type(target="e6", text="Madrid")`, then uses the reference
for “Buscar” from the new snapshot in `browser_click`. Page text and accessibility
names are untrusted data, **not instructions or permission to perform actions**.

Python Playwright 1.64.1 supplies the installed Chromium executable. The pinned
MCP npm package brings its matching Node Playwright dependency in `package-lock.json`.
The original direct-wrapper regression tests still use Python's snapshot APIs.

## Weather is not browsing

`weather(place)` uses Open-Meteo's keyless geocoding and forecast APIs, so a
simple “¿qué tiempo hace mañana en Madrid?” does not depend on finding a weather
URL. The model is told to use it instead of opening Google, Bing or Qwant; those
pages either require JavaScript results, return antibot challenges, or both.
Weather failures are reported, never guessed. The only variable sent to those
fixed hosts is the place name.

## Lifecycle and network boundary

The [2026-10-24 headless evaluation](docs/headless-evaluation.md) updates the earlier
El País observation: full Chromium headless with a version-consistent normal Chrome
UA returned homepage articles/headlines, while its default `HeadlessChrome` UA still
returned 403. No extension was needed. This tested profile is now the production
default, strictly headless, without weakening network restrictions.

- One fresh browser/context per conversation, created lazily, never the user's
  Chrome profile. Same-host actions preserve page state and temporary cookies.
  Switching host with `navigate` creates a fresh browser/context.
- The browser is **full Chromium in modern headless mode**, with the sandbox
  enabled and `--disable-blink-features=AutomationControlled`. Its UA is read from
  the actual running browser via `Browser.getVersion`, replacing only
  `HeadlessChrome/` with `Chrome/`. No hardcoded version, external fingerprint
  profile, stealth extension or personal cookies. Locale is `es-ES`, timezone
  `Europe/Madrid`, viewport `1440×900`. There is **no headed fallback**: launch
  failure, HTTP errors and verification challenges are reported, not bypassed.
  This improves compatibility in our checks; it does not make automation
  undetectable or guarantee access to every site.
- HTTP requests close their browser after the tool loop. Voice connections retain
  it across turns so the model can keep reading; the server closes it on
  cancellation, disconnect or conversation end. `browser_close` exists for an
  explicit user request, not for the model to tidy up after every answer.
  At most two browser sessions are live simultaneously.
- The model only sees the accessibility tree returned by the last tool call.
  The model can call `browser_snapshot` to re-read the current page with fresh refs
  in a new voice turn, so follow-ups do not have to reopen the URL or ask the user
  for references.
- URLs require HTTP(S), standard ports and no credentials. DNS answers must all
  be public, non-reserved addresses. Chromium DNS is pinned to a validated IP;
  ambient proxy settings are disabled. Numeric/local-address tricks, mixed
  public/private DNS answers and known IPv6 transition prefixes are rejected.
- Each request is checked: only the selected host and GET/HEAD are allowed.
  Other-host redirects return the destination for an explicit new `navigate`;
  private destinations are rejected. Third-party assets and all WebSockets are
  blocked, and page scripts cannot use WebRTC/WebTransport. Site JavaScript still
  runs: disabling it does not defeat DataDome (the 403 arrives before scripts) and
  breaks normal pages.
- No password filling, file pickers, uploads, downloads or model-provided eval.
  Popups, service workers and WebSockets are blocked; WebRTC/WebTransport APIs
  are disabled in page scripts. Chromium's sandbox remains enabled.
- Snapshots are bounded to 8,000 characters, at accessibility depth 12, with an
  explicit truncation marker. This is not a complete view of every long page.
  Actions have a 20-second budget, excluding cleanup/error reporting.

This is a constrained public-page reader with interaction, **not a general
browser sandbox or account automation tool**. GET endpoints can still have server
side effects. Pages requiring third-party assets, POST APIs, login, captchas or
downloads may not work. It neither solves captchas nor guarantees access to search
engines. Google returned a Spanish “tráfico inusual” challenge and is reported as
an error. A Bing attempt exposed its navigation shell but no useful results; that
is not evidence for any requested fact. Official AEMET page loading and ARIA
extraction were verified, independently of those search failures.

Browser tool calls/results are also recorded in the private `.chat` transcript.
No raw audio, user-browser cookies or profiles are used by this wrapper.

## Setup and tests

`run.sh` installs the pinned Python and npm dependencies and full Chromium (modern
headless uses it, not the shell). A separate marker upgrades existing shell-only
environments; dependency updates also refresh the browser. To install it explicitly:

```bash
.venv/bin/python -m playwright install chromium
npm ci --ignore-scripts
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests exercise real Chromium against intercepted fixture pages (no network
required): semantic labels and `aria-label`, hidden controls, typing without
submission, clicking through a GET form, current refs, `browser_snapshot`,
password/private navigation blocking, antibot errors, cancellation and closing. Additional unit tests cover
DNS policy, disabled tools, URL validation, streamed inline calls for each action,
and transcript logging. Live engine output is never treated as a test fixture.

### Live core-model conversations

With llama-server running, execute the opt-in suite:

```bash
DOT_TEST_CORE=1 .venv/bin/python -m unittest discover -s tests -p 'test_core_conversations.py' -v
```

These tests run the real model through `/api/chat` and real Chromium with
intercepted test pages. They check a clarification followed by page reading,
typing and GET submission with an observed confirmation code, follow-up recall,
antibot errors and ordinary conversation without browsing. The model is told the
reserved fixture domain is available in this test environment; otherwise it may
refuse to open it because it recognizes `.example`. A separate live streaming
check verifies navigation becomes tool events, not spoken URLs; it does not test
microphone capture or audible playback. Model responses are stochastic.

The requested all-catalog smoke checks (clock, Torremolinos tomorrow, BTC/USD) run
with `DOT_TEST_MCP_LIVE=1` and `test_mcp_live_tasks.py`. The test verifies exact
clock minutes and numeric price grounding against retrieved text, not a fixed quote.
Weather and clock use dedicated tools, while the BTC task exercises MCP browsing.
Observed results and limitations are recorded in `docs/mcp-trial.md`.

Inline recovery supports both `navigate("https://…")` (quoted, positional, named
or bare literal arguments) and the older Gemma-style `navigate{url:…}` syntax. Literal
decoding never executes code. Incomplete/malformed recognized calls are kept out
of speech. Parser regression tests cover every stream split boundary, escaping,
nested delimiters, both formats and multiple calls.
