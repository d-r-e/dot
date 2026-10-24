# Playwright MCP trial

Live checks on 2026-10-24 using Nemotron Bolt through dot's `/api/chat`, thinking
enabled, `DOT_MCP_ALL_TOOLS=1`, and all **72 native MCP schemas** exposed. Code,
uploads, private networking and other disallowed actions remained blocked by the
execution gateway. These are observed results, not deterministic test fixtures.

| Request | Observed answer/evidence | Tool | End-to-end time |
|---|---|---|---|
| “What time is it today?” | **07:54**, exactly matching the local Europe/Madrid clock (+02:00) on 2026-10-24 | `current_time` | 16.35 s |
| “What's the weather for tomorrow in Torremolinos?” | Sunday 25 October, cloudy, 15–23 °C, 0% precipitation, wind up to 10 km/h | `weather`, Open-Meteo | 10.01 s |
| “What is the current BTC value in USD?” | CoinMarketCap's displayed quote: **$80,651.48** at test time | Native MCP `browser_navigate` and snapshot | 20.53 s |

The clock and weather tasks deliberately use dedicated tools, not browser scraping.
The BTC result was present in the returned accessibility tree at
`https://www.coinmarketcap.com/currencies/bitcoin/`; this is a website quote,
not an independently verified exchange tick or a
guarantee of freshness. The short model answer omitted attribution, so the evidence
above records the source explicitly. Prices change; do not reuse this as a live quote.

Earlier trials also read CoinDesk successfully. Testing caught a clock answer that
changed the tool's minutes and a form action that used a field label instead of a
ref. Clock output/prompting now preserve HH:MM, and MCP schemas explicitly require
exact refs (including frame prefixes). The final three-prompt run above passed the
stronger clock/grounding assertions. A separate live-model form test navigated,
typed Madrid, submitted a GET search, read `NUBE-742` and recalled it on a follow-up.

Reproduce with a running llama-server:

```bash
DOT_TEST_MCP_LIVE=1 .venv/bin/python -m unittest discover -s tests -p 'test_mcp_live_tasks.py' -v
```

The full catalog adds prompt overhead even for simple requests. Normal operation
exposes only the eight approved browser tools plus emotion, weather and clock.
`DOT_MCP_ALL_TOOLS=1` is useful for evaluating selection, not bypassing policy.

Raw request/response traces are kept only under Git-ignored `logs/`; reasoning and
private transcript contents are not included here. Regression tests use intercepted
fixture pages with real MCP/Chromium, covering discovery, typing/clicking, blocked
tools, exact refs, password/private/cross-host navigation, HTTP errors, challenges,
and cancellation/reopening.
