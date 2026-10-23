import asyncio
import html
import json
import re
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import browser_tool
from browser_tool import BrowseError, BrowserSession, public_address, public_url
from tools import BLOCKING_TOOLS, InlineToolParser, Toolbox, tool_specs


class URLTests(unittest.TestCase):
    def test_public_url_normalization(self):
        self.assertEqual(public_url("https://EXAMPLE.COM.:443/path?q=1#fragment"),
                         ("https://example.com/path?q=1", "example.com"))
        self.assertEqual(public_url("https://8.8.8.8/")[1], "8.8.8.8")

    def test_forbidden_urls(self):
        for url in ["file:///etc/passwd", "data:text/html,hello", "http://localhost/",
                    "http://dot.local/", "http://127.0.0.1/", "http://10.0.0.1/",
                    "http://169.254.169.254/", "http://[::1]/", "http://[::ffff:8.8.8.8]/",
                    "http://[64:ff9b::7f00:1]/", "http://user:pass@example.com/",
                    "https://example.com:8765/", "https://example.com\\@localhost/",
                    "https://example.com/\n", "https://bad,host/"]:
            with self.subTest(url=url), self.assertRaises(BrowseError):
                public_url(url)

    def test_schemas_and_inline_recovery(self):
        with patch.dict("os.environ", {"DOT_TOOLS": "1", "DOT_BROWSE": "1"}):
            names = [spec["function"]["name"] for spec in tool_specs()]
        self.assertEqual(set(names), {"emotion", "weather", "browse", "navigate",
                                      "browser_click", "browser_type",
                                      "browser_snapshot", "browser_close"})
        for name, arguments in [("navigate", {"url": "https://example.com/"}),
                                ("browser_click", {"ref": "e12"}),
                                ("browser_type", {"ref": "e12", "text": "Madrid"}),
                                ("browser_snapshot", {}),
                                ("browser_close", {}),
                                ("weather", {"place": "Madrid"})]:
            parser = InlineToolParser(names)
            call = name + json.dumps(arguments)
            events = []
            for char in call:
                events.extend(parser.feed(char))
            events.extend(parser.feed("", final=True))
            calls = [event for event in events if event["type"] == "tool_call"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(calls[0]["arguments"]), arguments)
            self.assertIn(name, BLOCKING_TOOLS)
            self.assertFalse(any(event["type"] == "text" for event in events))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_private_and_mixed_answers(self):
        loop = asyncio.get_running_loop()
        def entry(ip):
            return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ips in [["127.0.0.1"], ["8.8.8.8", "192.168.1.1"]]:
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[entry(ip) for ip in ips])):
                with self.assertRaises(BrowseError):
                    await public_address("public-looking.example")
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[entry("8.8.8.8")])):
            self.assertEqual(await public_address("public-looking.example"), "8.8.8.8")

    async def test_snapshot_requires_an_open_page(self):
        with self.assertRaises(BrowseError):
            await BrowserSession().snapshot()

    async def test_request_guard(self):
        session = BrowserSession()
        frame = object()
        session._host = "public.example"
        session._page = SimpleNamespace(main_frame=frame)
        for url, method, allowed in [("https://public.example/page", "GET", True),
                                     ("https://public.example/api", "POST", False),
                                     ("http://127.0.0.1/", "GET", False),
                                     ("https://other.example/", "GET", False)]:
            route = SimpleNamespace(abort=AsyncMock(), continue_=AsyncMock(), request=SimpleNamespace(
                url=url, method=method, frame=frame, resource_type="document", is_navigation_request=lambda: True))
            await session._route(route)
            self.assertEqual(route.continue_.await_count, int(allowed))
            self.assertEqual(route.abort.await_count, int(not allowed))

    async def test_disabled_tools_do_not_open_browser(self):
        toolbox = Toolbox()
        with patch.dict("os.environ", {"DOT_TOOLS": "0"}), patch.object(toolbox.browser, "navigate", AsyncMock()) as navigate:
            self.assertTrue((await toolbox.call("navigate", {"url": "https://example.com/"})).startswith("error:"))
            navigate.assert_not_called()
        await toolbox.close()


class AccessibilityBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = BrowserSession()
        with patch("browser_tool.public_address", AsyncMock(return_value="8.8.8.8")):
            await self.session._open("fixture.example")

        async def fixture(route):
            if route.request.method != "GET":
                await route.fallback()
                return
            path = urlsplit(route.request.url).path
            if path == "/empty":
                document = "<body></body>"
            elif path == "/search":
                query = html.escape(parse_qs(urlsplit(route.request.url).query).get("q", [""])[0])
                document = f"<main><h1>Resultados</h1><p>Consulta recibida: {query}. Estos resultados son una prueba local del navegador y su árbol de accesibilidad.</p></main>"
            elif path == "/challenge":
                document = "<h1>Verifica que eres humano</h1><p>Este contenido no constituye una fuente de información verificada.</p>"
            else:
                document = """<title>Prueba accesible</title><main>
                    <h1>Prueba del árbol de accesibilidad</h1>
                    <p>Este documento controlado prueba la navegación, los nombres accesibles y el estado de los formularios sin usar la red.</p>
                    <form action='/search' method='get'><label for='q'>Consulta</label>
                    <input id='q' name='q'><button>Buscar</button></form>
                    <button type='button' aria-label='Abrir informe' onclick="document.getElementById('status').textContent='Informe listo'">Icono</button>
                    <button hidden>Oculto</button><p id='status' role='status'>Esperando</p>
                    <label for='password'>Clave privada</label><input id='password' type='password'>
                    <a href='http://127.0.0.1/'>Destino local prohibido</a>
                    </main>"""
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=document)

        await self.session._page.route("https://fixture.example/**", fixture)

    async def asyncTearDown(self):
        await self.session.close()

    def ref(self, snapshot, role, name):
        match = re.search(rf'{role} "{re.escape(name)}" \[ref=((?:f\d+)?e\d+)\]', snapshot)
        self.assertIsNotNone(match, snapshot)
        return match.group(1)

    async def test_aria_tree_type_click_and_close(self):
        view = await self.session.navigate("https://fixture.example/start")
        self.assertIn('heading "Prueba del árbol de accesibilidad"', view)
        self.assertNotIn("Oculto", view)
        query = self.ref(view, "textbox", "Consulta")
        view = await self.session.type(query, "clima de Madrid")
        self.assertIn("clima de Madrid", view)
        view = await self.session.click(self.ref(view, "button", "Abrir informe"))
        self.assertIn("Informe listo", view)
        view = await self.session.click(self.ref(view, "button", "Buscar"))
        self.assertIn("Consulta recibida: clima de Madrid", view)
        # No data-dot-ref CSS inventory: refs belong to Playwright's AX snapshot.
        self.assertIsNone(await self.session._page.query_selector("[data-dot-ref]"))
        with self.assertRaises(BrowseError):
            await self.session.type(query, "referencia de la página anterior")
        await self.session.close()
        self.assertIsNone(self.session._browser)
        self.assertFalse(self.session._leased)

    async def test_private_navigation_and_password_blocked(self):
        view = await self.session.navigate("https://fixture.example/start")
        with self.assertRaises(BrowseError):
            await self.session.type(self.ref(view, "textbox", "Clave privada"), "no enviar")
        with self.assertRaises(BrowseError):
            await self.session.click(self.ref(view, "link", "Destino local prohibido"))

    async def test_challenge_is_not_a_result(self):
        with self.assertRaisesRegex(BrowseError, "antibot"):
            await self.session.navigate("https://fixture.example/challenge")

    async def test_cancellation_closes_browser(self):
        task = asyncio.create_task(self.session.navigate("https://fixture.example/empty"))
        await self.session._page.wait_for_url("**/empty")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.session._browser)
        self.assertFalse(self.session._leased)


if __name__ == "__main__":
    unittest.main()
