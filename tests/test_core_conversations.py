"""Opt-in integration tests: DOT_TEST_CORE=1, running llama-server required.

Real core model and application tool loop, real Chromium with intercepted pages.
No model responses or tool decisions are mocked. Fixtures make action outcomes
verifiable without depending on external sites or search-engine availability.
"""

import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

import app
from browser_tool import BrowserSession
from tools import tool_specs


@unittest.skipUnless(os.environ.get("DOT_TEST_CORE") == "1", "requires live core model")
class CoreConversations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actions = []
        original_open = BrowserSession._open
        actions = self.actions

        async def open_fixture(session, host):
            with patch("browser_tool.public_address", AsyncMock(return_value="8.8.8.8")):
                await original_open(session, host)

            async def fixture(route):
                parsed = urlsplit(route.request.url)
                if route.request.method != "GET":
                    await route.fallback()
                    return
                actions.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
                if parsed.path == "/challenge":
                    body = '<h1>Verifica que eres humano</h1><p>Checking your browser. No hay resultados disponibles.</p>'
                elif parsed.path == "/search":
                    query = parse_qs(parsed.query).get("q", [""])[0]
                    body = f'<main><h1>Resultados</h1><p>Consulta recibida: {query}. Código de confirmación: NUBE-742. Esta página confirma que el formulario se ha enviado correctamente.</p></main>'
                else:
                    body = '''<main><h1>Biblioteca de prueba</h1>
                    <p>La biblioteca abre los sábados a las 10:30. Este es el horario publicado para la prueba de navegación del asistente.</p>
                    <form action="/search" method="get"><label for="q">Consulta</label>
                    <input id="q" name="q"><button>Buscar</button></form></main>'''
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

            await session._page.route("https://fixture.example/**", fixture)

        self.patches = [patch.object(BrowserSession, "_open", open_fixture),
                        patch.object(app.manager, "snapshot", return_value={"state": "ready"}),
                        patch.dict(os.environ, {"DOT_TOOLS": "1", "DOT_BROWSE": "1"})]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://localhost")

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    async def turn(self, history, text):
        if not history:
            history.append({"role": "system", "content": app.system_prompt() +
                            " En esta prueba, fixture.example es un sitio de pruebas servido "
                            "por el navegador disponible. Ábrelo para observar su contenido; "
                            "no supongas que no existe por su nombre."})
        history.append({"role": "user", "content": text})
        response = await self.client.post("/api/chat", json={"messages": list(history), "max_tokens": 1024})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        answer = data["choices"][0]["message"].get("content") or ""
        trace = data.get("dot_tools", [])
        print(json.dumps({"user": text, "assistant": answer,
                          "tools": [{"name": t["name"], "arguments": t["arguments"],
                                     "error": t["result"][:160] if t["result"].startswith("error:") else None}
                                    for t in trace]}, ensure_ascii=False), flush=True)
        self.assertNotRegex(answer, r'(?:browse|navigate|browser_\w+)\s*[({]')
        self.assertNotIn("https://", answer)
        history.append({"role": "assistant", "content": answer})
        return answer, trace

    async def test_read_page_after_clarification(self):
        history = []
        await self.turn(history, "Quiero comprobar el horario de una biblioteca. Te paso la página en el siguiente mensaje.")
        answer, trace = await self.turn(history, "Abre https://fixture.example/library y dime a qué hora abre los sábados.")
        self.assertTrue(any(t["name"] in {"navigate", "browse"} for t in trace))
        self.assertIn("10:30", answer)
        self.assertIn("/library", self.actions)

    async def test_fill_and_submit_then_follow_up(self):
        history = []
        answer, trace = await self.turn(history, "Abre https://fixture.example/library, escribe Madrid en Consulta, pulsa Buscar y dime el código de confirmación que aparece en los resultados.")
        names = [t["name"] for t in trace]
        self.assertIn("browser_type", names)
        self.assertIn("browser_click", names)
        self.assertIn("/search?q=Madrid", self.actions)
        self.assertIn("NUBE-742", answer)
        answer, _ = await self.turn(history, "Repíteme solo el código que acabas de obtener.")
        self.assertIn("NUBE-742", answer)

    async def test_blocked_page_no_fabricated_result(self):
        answer, trace = await self.turn([], "Consulta https://fixture.example/challenge y dime qué noticias de Madrid publica hoy.")
        self.assertTrue(any("antibot" in t["result"] for t in trace))
        self.assertRegex(answer.lower(), r"no (?:puedo|he podido)|verifica|bloque|captcha")

    async def test_weather_without_search_or_url(self):
        history = []
        answer, trace = await self.turn(history, "¿Qué tiempo hace mañana en Madrid?")
        self.assertTrue(any(t["name"] == "weather" for t in trace), trace)
        self.assertIn("°C", answer)
        self.assertNotRegex(answer.lower(), r"necesito una url|no puedo acceder|no tengo acceso")
        answer, trace = await self.turn(history, "¿Y pasado mañana en Valencia?")
        self.assertTrue(any(t["name"] == "weather" for t in trace), trace)
        self.assertIn("Valencia", answer)

    async def test_no_browser_for_simple_conversation(self):
        history = []
        await self.turn(history, "Hola, responde brevemente en español.")
        _, trace = await self.turn(history, "¿Cuánto es dos más dos? No necesitas internet.")
        self.assertFalse(any(t["name"] != "emotion" for t in trace))
        self.assertEqual(self.actions, [])

    async def test_voice_stream_calls_weather_without_speaking_the_call(self):
        events = [event async for event in app.stream_llm([
            {"role": "system", "content": app.system_prompt()},
            {"role": "user", "content": "¿Qué tiempo hace mañana en Madrid?"},
        ], max_tokens=1024, tools=tool_specs())]
        calls = [event for event in events if event["type"] == "tool_call"]
        speech = ''.join(event["text"] for event in events if event["type"] == "text")
        print(json.dumps({"weather_speech": speech, "calls": calls}, ensure_ascii=False), flush=True)
        self.assertTrue(any(e["name"] == "weather" for e in calls), events)
        self.assertNotIn("weather", speech)
        self.assertNotIn("°C", speech)

    async def test_voice_stream_recovers_navigation_without_speaking_url(self):
        events = [event async for event in app.stream_llm([
            {"role": "system", "content": app.system_prompt()},
            {"role": "user", "content": "Abre https://fixture.example/library y comprueba el horario de los sábados."},
        ], max_tokens=1024, tools=tool_specs())]
        calls = [event for event in events if event["type"] == "tool_call"]
        speech = ''.join(event["text"] for event in events if event["type"] == "text")
        print(json.dumps({"voice_speech": speech, "calls": calls}, ensure_ascii=False), flush=True)
        self.assertTrue(any(e["name"] in {"navigate", "browse"} for e in calls), events)
        self.assertNotIn("https://", speech)
        self.assertNotRegex(speech, r'(?:browse|navigate)\s*[({]')
