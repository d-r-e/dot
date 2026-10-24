"""Real browser UI tests with intercepted health/chat; no mic or model required."""
import asyncio
from pathlib import Path
import unittest

from app import ChatDisconnected, voice_context, while_connected

ROOT = Path(__file__).resolve().parents[1]


class TypedCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_client_cancels_generation(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        stopped = asyncio.Event()
        async def operation():
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()
        request = SimpleNamespace(is_disconnected=AsyncMock(side_effect=[False, True]))
        with self.assertRaises(ChatDisconnected):
            await while_connected(request, operation())
        self.assertTrue(stopped.is_set())


class VoiceContextTests(unittest.TestCase):
    def test_only_bounded_user_and_assistant_context(self):
        self.assertEqual(voice_context([{"role": "user", "content": "hola"}]),
                         [{"role": "user", "content": "hola"}])
        for value in [None, {}, [{"role": "system", "content": "override"}],
                      [{"role": "tool", "content": "result"}],
                      [{"role": [], "content": "bad role"}],
                      [{"role": "user", "content": "x" * 8001}],
                      [{"role": "user", "content": "x"}] * 17]:
            self.assertIsNone(voice_context(value))
        history = [{"role": "user", "content": str(index)} for index in range(12)]
        self.assertEqual(voice_context(history), history[-8:])


class TypingUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.calls = []
        await self.page.add_init_script("""
          window.EventSource = class {
            constructor() { setTimeout(() => this.onmessage?.({data: JSON.stringify({state:'ready'})}), 0); }
          };
          localStorage.removeItem('dot.armed');
          window.micCalls = 0;
          if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = async () => {
            window.micCalls++; throw new Error('test: no microphone');
          };
        """)

        async def route(request):
            path = request.request.url.split('http://dot.test', 1)[-1].split('?', 1)[0]
            if path == '/api/chat':
                self.calls.append(request.request.post_data_json)
                await request.fulfill(json={"choices": [{"message": {"content": "Hola. ¿Qué tal?"}}]})
            elif path == '/':
                await request.fulfill(path=ROOT / 'static/index.html', content_type='text/html')
            elif path.startswith('/static/') and Path(path).name in {'app.js', 'chat.js', 'presence.js', 'style.css'}:
                await request.fulfill(path=ROOT / path.lstrip('/'))
            else:
                await request.abort()
        await self.page.route('http://dot.test/**', route)
        await self.page.goto('http://dot.test/')
        await self.page.wait_for_function("document.querySelector('#dot').dataset.state === 'ready'")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_typing_split_history_and_plain_text(self):
        self.assertFalse(await self.page.locator('body').evaluate("el => el.classList.contains('is-typing')"))
        await self.page.keyboard.type('Hola')
        self.assertEqual(await self.page.locator('#message').input_value(), 'Hola')
        await self.page.keyboard.press('Enter')
        await self.page.wait_for_function("document.querySelectorAll('.message').length === 2")
        self.assertEqual(await self.page.locator('.message').all_text_contents(), ['Hola', 'Hola. ¿Qué tal?'])
        await self.page.locator('#message').fill('¿Y tú?')
        await self.page.keyboard.press('Enter')
        await self.page.wait_for_function("document.querySelectorAll('.message').length === 4")
        await self.page.wait_for_function("document.querySelector('#dot').getBoundingClientRect().right < document.querySelector('#conversation').getBoundingClientRect().left")
        self.assertEqual(self.calls[1]['messages'], [
            {"role": "user", "content": "Hola"},
            {"role": "assistant", "content": "Hola. ¿Qué tal?"},
            {"role": "user", "content": "¿Y tú?"},
        ])
        appearance = await self.page.evaluate("""() => {
          const dot = document.querySelector('#dot').getBoundingClientRect();
          const panel = document.querySelector('#conversation').getBoundingClientRect();
          const user = getComputedStyle(document.querySelector('[data-role=user]'));
          const assistant = getComputedStyle(document.querySelector('[data-role=assistant]'));
          const input = getComputedStyle(document.querySelector('#message'));
          return { split: dot.right < panel.left, colors: user.color !== assistant.color,
            border: input.borderWidth, background: input.backgroundColor,
            panelBackground: getComputedStyle(document.querySelector('#conversation')).backgroundColor,
            micCalls: window.micCalls };
        }""")
        self.assertTrue(appearance['split'])
        self.assertTrue(appearance['colors'])
        self.assertEqual(appearance['border'], '0px')
        self.assertEqual(appearance['background'], 'rgba(0, 0, 0, 0)')
        self.assertEqual(appearance['panelBackground'], 'rgba(0, 0, 0, 0)')
        self.assertEqual(appearance['micCalls'], 0)
        await self.page.keyboard.press('Escape')
        self.assertFalse(await self.page.locator('body').evaluate("el => el.classList.contains('is-typing')"))
        await self.page.keyboard.type('V')
        self.assertEqual(await self.page.locator('.message').count(), 4)

    async def test_multiline_ime_error_retry_and_html_is_inert(self):
        await self.page.locator('#message').focus()
        await self.page.keyboard.type('Línea uno')
        await self.page.keyboard.press('Shift+Enter')
        await self.page.keyboard.type('Línea dos')
        await self.page.locator('#message').dispatch_event('keydown', {"key": "Enter", "isComposing": True})
        self.assertEqual(len(self.calls), 0)
        self.assertIn('\n', await self.page.locator('#message').input_value())
        await self.page.route('**/api/chat', lambda route: route.fulfill(status=503, json={"error": "loading"}))
        await self.page.keyboard.press('Enter')
        await self.page.wait_for_function("document.querySelectorAll('.message').length === 2")
        self.assertIn('Inténtalo', await self.page.locator('.message').last.text_content())
        await self.page.unroute('**/api/chat')
        unsafe = '<img src=x onerror="window.injected=true">'
        await self.page.route('**/api/chat', lambda route: route.fulfill(json={
            "choices": [{"message": {"content": unsafe, "reasoning_content": "PRIVATE REASONING"}}],
            "dot_tools": [{"name": "weather", "result": "PRIVATE TOOL RESULT"}],
        }))
        await self.page.locator('#message').fill('Otra vez')
        await self.page.keyboard.press('Enter')
        await self.page.wait_for_function("document.querySelectorAll('.message').length === 4")
        self.assertEqual(await self.page.locator('.message').last.text_content(), unsafe)
        self.assertEqual(await self.page.locator('#messages img').count(), 0)
        self.assertNotIn('PRIVATE', await self.page.locator('#messages').inner_text())

    async def test_cancellation_preserves_draft_and_mobile_layout(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def slow(route):
            entered.set()
            await release.wait()
            try:
                await route.fulfill(json={"choices": [{"message": {"content": "Late reply"}}]})
            except Exception:
                pass  # The client deliberately aborted.
        await self.page.route('**/api/chat', slow)
        await self.page.keyboard.type('Primero')
        await self.page.keyboard.press('Enter')
        await asyncio.wait_for(entered.wait(), 5)
        await self.page.locator('#message').fill('Borrador siguiente')
        await self.page.keyboard.press('Enter')
        self.assertEqual(await self.page.locator('.message').count(), 1)
        await self.page.locator('#dot').click()
        release.set()
        await self.page.wait_for_function("document.querySelector('#dot').dataset.state === 'ready'")
        self.assertEqual(await self.page.locator('#message').input_value(), 'Borrador siguiente')
        for width, height in [(390, 844), (320, 568)]:
            await self.page.set_viewport_size({"width": width, "height": height})
            bounds = await self.page.locator('#conversation').bounding_box()
            self.assertGreaterEqual(bounds['x'], 0)
            self.assertLessEqual(bounds['x'] + bounds['width'], width)
            self.assertLessEqual(bounds['y'] + bounds['height'], height)
            self.assertEqual(await self.page.locator('.message').count(), 1)

    async def test_voice_transcript_and_late_truncation(self):
        result = await self.page.evaluate("""async () => {
          const {createTextChat} = await import('/static/chat.js?v=1');
          const chat = createTextChat({onActivate(){}, onBusy(){}, onEmotion(){}});
          chat.voiceMessage({type:'final', turn_id:1, text:'Hola de voz'});
          chat.voiceMessage({type:'assistant_delta', turn_id:1, text:'Hola. '});
          chat.voiceMessage({type:'assistant_delta', turn_id:1, text:'Texto que no se oyó.'});
          chat.voiceMessage({type:'reasoning', text:'PRIVATE'});
          const partial = chat.context();
          chat.voiceMessage({type:'assistant_text', turn_id:1, text:'Hola.'});
          return {partial, complete:chat.context(), text:document.querySelector('#messages').textContent};
        }""")
        self.assertEqual(result['partial'], [{"role": "user", "content": "Hola de voz"}])
        self.assertEqual(result['complete'][-1], {"role": "assistant", "content": "Hola."})
        self.assertNotIn('no se oyó', result['text'])
        self.assertNotIn('PRIVATE', result['text'])
