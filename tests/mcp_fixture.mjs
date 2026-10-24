// Deterministic intercepted pages: no external network, no model responses mocked.
import { startGateway } from '../mcp_browser.mjs';
import { chromium } from 'playwright';

let launchOptions;
const nativeLaunch = chromium.launch.bind(chromium);
chromium.launch = options => { launchOptions = options; return nativeLaunch(options); };

await startGateway({ configureContext: async context => {
  // A page route takes precedence over the context's network guard only for the
  // reserved fixture host. Other requests still cross the production guard.
  context.on('page', page => page.route('https://fixture.example/**', async route => {
    if (route.request().method() !== 'GET') return route.fallback();
    const url = new URL(route.request().url());
    let body;
    if (url.pathname === '/profile') {
      const metadata = JSON.stringify({
        headless: launchOptions.headless,
        sandbox: launchOptions.chromiumSandbox,
        fullChromium: !launchOptions.executablePath.includes('headless-shell'),
        automationFlag: launchOptions.args.includes('--disable-blink-features=AutomationControlled'),
        actualMajor: context.browser().version().split('.')[0],
        requestUA: route.request().headers()['user-agent'],
      });
      body = `<pre id="profile"></pre><script>
        document.querySelector('#profile').textContent = JSON.stringify({
          ...${metadata}, userAgent:navigator.userAgent, webdriver:navigator.webdriver,
          language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
          width:innerWidth, height:innerHeight
        });
      </script>`;
    } else if (url.pathname === '/challenge')
      body = '<h1>Verifica que eres humano</h1><p>Checking your browser.</p>';
    else if (url.pathname === '/search') {
      const query = (url.searchParams.get('q') || '').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      body = `<main><h1>Resultados</h1><p>Consulta recibida: ${query}. Código de confirmación: NUBE-742.</p></main>`;
    } else if (url.pathname === '/http-error')
      return route.fulfill({ status: 403, contentType: 'text/html', body: '<h1>Forbidden</h1>' });
    else if (url.pathname === '/slow')
      return; // Deliberately pending request for cancellation coverage.
    else
      body = `<main><h1>Biblioteca de prueba</h1>
        <p>La biblioteca abre los sábados a las 10:30. Este es el horario publicado.</p>
        <form action='/search' method='get'><label for='q'>Consulta</label>
        <input id='q' name='q'><button>Buscar</button></form>
        <label for='pw'>Clave privada</label><input id='pw' type='password'>
        <a href='http://127.0.0.1/'>Destino local prohibido</a>
        <a href='https://elsewhere.example/'>Otro dominio</a>
        <form action='/post' method='post'><button>Enviar POST</button></form>
        </main>`;
    await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body });
  }));
} });
