# Headless + Playwright MCP evaluation

Observed on 2026-10-24, macOS, Chromium 156, using the production MCP gateway and
its public/pinned DNS, same-host GET/HEAD, sandbox, isolated-context and no-file
policies. No extensions, personal profiles, CAPTCHA solving or proxy changes.
These are small live checks, not an access guarantee or a sustained benchmark.

## El País homepage

| Configuration | Result |
|---|---|
| Headed/minimized, native Chrome UA, `webdriver=true` | Homepage snapshot |
| Full Chromium headless, native `HeadlessChrome` UA | HTTP 403 |
| Headless + `--disable-blink-features=AutomationControlled` | HTTP 403, despite `webdriver=false` |
| Headless + native UA with only `HeadlessChrome/` replaced by `Chrome/` | Homepage, articles and headlines; `webdriver=true` |
| Headless + normalized UA + automation flag | Homepage, articles and headlines; `webdriver=false`; confirmed twice |

The UA was derived from `Browser.getVersion` for the installed browser, not a
hardcoded version or a third-party fingerprint profile. A final fresh-context
control with the original `HeadlessChrome` UA returned 403 again. The successful
snapshots included `main`, articles and multiple level-2 headings, not just a title
or an empty navigation shell. Normalized-UA checks took about 0.9–1.6 seconds
end-to-end, but order, caching and network variation prevent a speedup claim.

## Other homepages

El Confidencial, CoinDesk and Spanish Wikipedia returned accessibility snapshots
with headed, plain headless, headless + automation flag, and headless + flag +
normalized UA. They did not need an additional extension in these checks.

## Interpretation

- For El País in this environment, changing the UA alone was sufficient; hiding
  `navigator.webdriver` alone was not. This does not establish every signal used by
  the site's anti-automation system.
- This updates the earlier observation that the tested headless variants failed.
  That observation must not be treated as a universal restriction on headless MCP.
- Modern headless uses full Chrome/Chromium, not `chrome-headless-shell`.
  Playwright's `channel: "chromium"` selects the modern mode; dot's experiment used
  the installed full Chromium executable explicitly.
- Playwright MCP's normal launch configuration adds the automation flag, but dot's
  trusted `contextGetter` launches Chromium itself for network enforcement. Thus
  that default does not automatically apply to our browser.
- The official Playwright extension attaches to an existing browser/profile; it
  does not promise stealth or make a headless browser undetectable. Do not attach
  dot to a personal logged-in profile.

## Applied configuration

Following the evaluation, the requested production profile is now **strictly
headless**: full Chromium, native/version-consistent UA with `HeadlessChrome/`
replaced by `Chrome/`, and `--disable-blink-features=AutomationControlled`.
Locale/timezone/viewport remain `es-ES`, `Europe/Madrid`, `1440×900`. The sandbox,
pinned public DNS and request restrictions remain enabled. There are no extensions,
proxies or personal profiles, and **no headed/minimized fallback**. Failures remain
explicit; do not silently retry arbitrary interactions or treat challenges as facts.

The applied production gateway (not the experiment wrapper) was then checked
against the same four URLs: all returned page snapshots, including El País's
`main`, articles and headlines. El Confidencial's bounded snapshot was dominated
by navigation, so this is an access check, not a claim that all its articles fit
in the returned tree. Regression coverage verifies headless/full-executable launch,
sandbox, `webdriver=false`, version-consistent matching HTTP/JavaScript UA,
locale/timezone/viewport, and the unchanged network/interaction restrictions.
The suite passed: 53 tests run, 8 opt-in tests skipped.

Raw observations and the local probe are under Git-ignored `logs/`:
`mcp-headless-results.json`, `mcp-headless-confirmation.json`, and
`mcp_headless_probe.mjs` (historical probe of the pre-change gateway).
The post-change access check is `headless-production-smoke.json` in the same directory.

Sources: [Playwright browsers](https://playwright.dev/docs/browsers),
[Chrome headless](https://developer.chrome.com/docs/chromium/headless),
[Playwright MCP](https://github.com/microsoft/playwright-mcp),
[official extension](https://github.com/microsoft/playwright/tree/main/packages/extension).
