# Browsing and accessibility refs

## Why replace web_search?

Live validation on 2026-10-23 used the existing implementation with an AEMET
weather query and a scoped documentation query. Both failed: DuckDuckGo returned
an HTTP 202 antibot challenge on each attempt; Brave returned 429, including its
retry. Neither supplied usable results. The curl scrapers and search cache have
been removed rather than presented as reliable search.

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `browse` / `navigate` | `url` | Navigate and return an accessibility snapshot |
| `browser_click` | `ref` | Click the referenced element and return a fresh snapshot |
| `browser_type` | `ref`, `text` | Fill a field; no Enter or automatic submission |
| `browser_snapshot` | none | Return the current view again with fresh refs |
| `browser_close` | none | Close the browser; only on an explicit user request |
| `weather` | `place` | Open-Meteo current conditions and three-day forecast |

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
- `page.get_by_ref(ref)` resolves those references to actual Playwright locators.
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

The model calls `browser_type(ref="e6", text="Madrid")`, then uses the reference
for “Buscar” from the new snapshot in `browser_click`. Page text and accessibility
names are untrusted data, **not instructions or permission to perform actions**.

The Python dependency is pinned to Playwright 1.64.1, which exposes these APIs
publicly; no private protocol hooks or separate MCP server are required.

## Weather is not browsing

`weather(place)` uses Open-Meteo's keyless geocoding and forecast APIs, so a
simple “¿qué tiempo hace mañana en Madrid?” does not depend on finding a weather
URL. The model is told to use it instead of opening Google, Bing or Qwant; those
pages either require JavaScript results, return antibot challenges, or both.
Weather failures are reported, never guessed. The only variable sent to those
fixed hosts is the place name.

## Lifecycle and network boundary

- One fresh browser/context per conversation, created lazily, never the user's
  Chrome profile. Same-host actions preserve page state and temporary cookies.
  Switching host with `navigate` creates a fresh browser/context.
- The browser is **headed Chromium with its window minimized** through CDP. Some
  sites, including `elpais.com` (DataDome), return 403 to every headless Chromium
  variant; a headed browser gets the normal page. If no GUI session exists (ssh,
  CI), the tool falls back to headless and those sites stay blocked.
- HTTP requests close their browser after the tool loop. Voice connections retain
  it across turns so the model can keep reading; the server closes it on
  cancellation, disconnect or conversation end. `browser_close` exists for an
  explicit user request, not for the model to tidy up after every answer.
  At most two browser sessions are live simultaneously.
- The model only sees the accessibility tree returned by the last tool call.
  `browser_snapshot` re-reads the current page with fresh refs at the start of a
  new voice turn, so follow-ups do not have to reopen the URL or ask the user for
  references.
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

`run.sh` installs the pinned Python dependency and the full Chromium build when
bootstrapping/updating dependencies (the headed browser needs it). To install the
browser explicitly:

```bash
.venv/bin/python -m playwright install chromium
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

Inline recovery supports both `navigate("https://…")` (quoted, positional, named
or bare literal arguments) and the older Gemma-style `navigate{url:…}` syntax. Literal
decoding never executes code. Incomplete/malformed recognized calls are kept out
of speech. Parser regression tests cover every stream split boundary, escaping,
nested delimiters, both formats and multiple calls.
