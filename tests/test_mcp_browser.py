import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from browser_tool import BrowseError
from mcp_browser import MCPBrowserSession, MCPConnection
from tools import Toolbox


class MCPCatalogTests(unittest.TestCase):
    def test_full_catalog_switch_does_not_change_permissions(self):
        from mcp_browser import SAFE_TOOLS, browser_specs
        catalog = [{"type": "function", "function": {"name": name, "parameters": {}}}
                   for name in ['browser_navigate', 'browser_run_code_unsafe']]
        with patch('mcp_browser.MCP_CATALOG', catalog):
            with patch.dict('os.environ', {'DOT_MCP_ALL_TOOLS': '0'}):
                self.assertEqual([s['function']['name'] for s in browser_specs()], ['browser_navigate'])
            with patch.dict('os.environ', {'DOT_MCP_ALL_TOOLS': '1'}):
                self.assertEqual(len(browser_specs()), 2)
                self.assertNotIn('browser_run_code_unsafe', SAFE_TOOLS)


class MCPBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.script_patch = patch.object(MCPConnection, 'server_script', Path(__file__).with_name('mcp_fixture.mjs'))
        self.dns_patch = patch('mcp_browser.public_address', AsyncMock(return_value='8.8.8.8'))
        self.script_patch.start()
        self.dns_patch.start()
        self.session = MCPBrowserSession()

    async def asyncTearDown(self):
        await self.session.close()
        self.dns_patch.stop()
        self.script_patch.stop()

    def ref(self, view, role, name):
        match = re.search(rf'{role} "{name}" \[ref=((?:f\d+)?e\d+)\]', view)
        self.assertIsNotNone(match, view)
        return match[1]

    async def test_real_mcp_discovery_read_type_click(self):
        connection = MCPConnection()
        try:
            names = {tool['function']['name'] for tool in await connection.start()}
            self.assertIn('browser_run_code_unsafe', names)
            self.assertIn('browser_navigate', names)
            self.assertGreater(len(names), 30)
            # Gate rejects unsafe calls even when bypassing Toolbox completely.
            result = await connection.call('browser_evaluate', {'function': '() => 1'})
            self.assertIn('bloqueada', result)
        finally:
            await connection.close()
        view = await self.session.navigate('https://fixture.example/library')
        self.assertIn('10:30', view)
        view = await self.session.type(self.ref(view, 'textbox', 'Consulta'), 'Madrid')
        self.assertIn('Madrid', view)
        view = await self.session.click(self.ref(view, 'button', 'Buscar'))
        self.assertIn('NUBE-742', view)
        self.assertIn('/search?q=Madrid', view)

    async def test_policy_boundaries(self):
        for url in ['file:///etc/passwd', 'http://127.0.0.1/', 'http://10.0.0.1/']:
            with self.assertRaises(BrowseError):
                await self.session.navigate(url)
        view = await self.session.navigate('https://fixture.example/library')
        for name, args in [
            ('browser_type', {'target': self.ref(view, 'textbox', 'Clave privada'), 'text': 'secret'}),
            ('browser_click', {'target': 'a'}),
            ('browser_snapshot', {'filename': 'private.yml'}),
            ('browser_type', {'target': self.ref(view, 'textbox', 'Consulta'), 'text': 'Madrid', 'submit': True}),
        ]:
            result = await self.session.call(name, args)
            self.assertIn('error:', result)
        for label in ['Destino local prohibido', 'Otro dominio']:
            view = await self.session.navigate('https://fixture.example/library')
            result = await self.session.click(self.ref(view, 'link', label))
            self.assertIn('bloqueada', result)
        view = await self.session.navigate('https://fixture.example/library')
        result = await self.session.click(self.ref(view, 'button', 'Enviar POST'))
        self.assertIn('bloqueada', result)

    async def test_http_errors_challenges_and_cancellation(self):
        result = await self.session.navigate('https://fixture.example/http-error')
        self.assertIn('403', result)
        result = await self.session.navigate('https://fixture.example/challenge')
        self.assertIn('antibot', result)
        task = asyncio.create_task(self.session.navigate('https://fixture.example/slow'))
        await asyncio.sleep(0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.session.connection)
        self.assertFalse(self.session.leased)
        view = await self.session.navigate('https://fixture.example/library')
        self.assertIn('10:30', view)

    async def test_current_time_is_local_and_timezone_aware(self):
        from datetime import datetime
        value = await Toolbox().call('current_time', {})
        timestamp = datetime.fromisoformat(value.split(' ')[0])
        self.assertIsNotNone(timestamp.utcoffset())
        self.assertIn('Europe/Madrid', value)

    async def test_strict_headless_profile_matches_installed_chromium(self):
        view = await self.session.navigate('https://fixture.example/profile')
        # The page renders its real navigator/viewport plus captured launch options.
        self.assertNotIn('error:', view)
        scalar = re.search(r'\[ref=[^\]]+\]: (".*")$', view, re.MULTILINE).group(1)
        profile = json.loads(json.loads(scalar))
        for flag in ['headless', 'sandbox', 'fullChromium', 'automationFlag']:
            self.assertTrue(profile[flag])
        self.assertFalse(profile['webdriver'])
        self.assertEqual(profile['language'], 'es-ES')
        self.assertEqual(profile['timezone'], 'Europe/Madrid')
        self.assertEqual((profile['width'], profile['height']), (1440, 900))
        self.assertNotIn('HeadlessChrome', profile['userAgent'])
        self.assertEqual(profile['requestUA'], profile['userAgent'])
        self.assertIn(f"Chrome/{profile['actualMajor']}.", profile['userAgent'])
