"""Opt-in real-model/public-network smoke: DOT_TEST_MCP_LIVE=1.

These checks exercise HTTP text/tool orchestration, not microphones or playback.
Observed prices/forecasts are never hardcoded into assertions.
"""
import json
import os
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch

import httpx

import app
from tools import prepare_tools


@unittest.skipUnless(os.environ.get('DOT_TEST_MCP_LIVE') == '1', 'requires live model and public network')
class MCPLiveTasks(unittest.IsolatedAsyncioTestCase):
    async def test_requested_tasks_with_full_catalog(self):
        results = []
        with patch.dict(os.environ, {'DOT_TOOLS': '1', 'DOT_BROWSE': '1', 'DOT_MCP_ALL_TOOLS': '1'}), \
             patch.object(app.manager, 'snapshot', return_value={'state': 'ready'}):
            await prepare_tools()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://localhost', timeout=300) as client:
                for prompt, expected in [
                    ('What time is it today?', {'current_time'}),
                    ("What's the weather for tomorrow in Torremolinos?", {'weather'}),
                    ('What is the current BTC value in USD?', {'browser_navigate', 'browse', 'navigate'}),
                ]:
                    started = time.monotonic()
                    response = await client.post('/api/chat', json={'prompt': prompt})
                    self.assertEqual(response.status_code, 200, response.text)
                    data = response.json()
                    answer = data['choices'][0]['message'].get('content') or ''
                    trace = data.get('dot_tools', [])
                    results.append({'prompt': prompt, 'answer': answer, 'elapsed_s': round(time.monotonic()-started, 2),
                                    'tools': trace})
                    path = Path('logs/mcp-requested-tasks.json')
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
                    print(json.dumps({k: v for k, v in results[-1].items() if k != 'tools'}, ensure_ascii=False), flush=True)
                    self.assertTrue(answer.strip(), data)
                    self.assertTrue(any(t['name'] in expected and not t['result'].startswith('error:') for t in trace), trace)
                    if expected == {'current_time'}:
                        result = next(t['result'] for t in trace if t['name'] == 'current_time')
                        hhmm = re.search(r'T(\d{2}:\d{2}):', result).group(1)
                        self.assertIn(hhmm, answer, 'Clock answer must preserve tool minutes exactly')
                    if 'browser_navigate' in expected:
                        # A numeric quote in the answer must occur in the actual
                        # retrieved text (allowing Spanish/English punctuation).
                        def numbers(text):
                            return {re.sub(r'\D', '', number) for number in re.findall(r'\d[\d,.]*\d', text)}
                        retrieved = '\n'.join(t['result'] for t in trace)
                        self.assertTrue(numbers(answer) & numbers(retrieved), answer)
