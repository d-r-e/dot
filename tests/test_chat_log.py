import asyncio
import json
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
from chat_log import ChatLog


class ChatLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / ".chat"
        self.log = ChatLog(self.path)
        self.log.start()

    def tearDown(self):
        self.log.close()
        self.temp.cleanup()

    def records(self):
        self.log.close()
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_unicode_newlines_snapshot_and_private_permissions(self):
        context = {"text": "¿Cómo estás?\nEstoy aquí."}
        self.log.emit("session", "message", role="user", context=context)
        context["text"] = "changed later"
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["context"]["text"], "¿Cómo estás?\nEstoy aquí.")
        self.assertIn("timestamp", records[0])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_concurrent_writes_and_shutdown_drain(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda number: self.log.emit(str(number % 4), "message", number=number), range(200)))
        records = self.records()
        self.assertEqual({record["number"] for record in records}, set(range(200)))

    def test_restart_appends(self):
        self.log.emit("one", "message", text="Primera conversación")
        self.log.close()
        self.log.start()
        self.log.emit("two", "message", text="Segunda conversación")
        self.assertEqual([record["session"] for record in self.records()], ["one", "two"])

    def test_unwritable_path_does_not_break_conversation(self):
        broken = ChatLog(Path(self.temp.name))  # A directory, not a writable file.
        with self.assertLogs("uvicorn.error", level="ERROR"):
            broken.start()
        broken.emit("test", "message", text="Sigue funcionando")
        broken.close()

    def test_malformed_unicode_does_not_kill_writer(self):
        self.log.emit("test", "message", text="\ud800")
        self.log.emit("test", "message", text="Continúa")
        self.assertEqual(len(self.records()), 2)

    def test_http_conversation(self):
        request = SimpleNamespace(headers={}, json=AsyncMock(return_value={"prompt": "Hola"}))
        answer = {"choices": [{"message": {"role": "assistant", "content": "Hola, ¿cómo estás?"}}]}
        with patch.object(app, "transcript", self.log), \
             patch.object(app.manager, "snapshot", return_value={"state": "ready"}), \
             patch.object(app, "_chat_once", AsyncMock(return_value=answer)):
            response = asyncio.run(app.chat(request))
        self.assertEqual(response.status_code, 200)
        records = self.records()
        self.assertEqual([record["text"] for record in records if record["event"] == "message"],
                         ["Hola", "Hola, ¿cómo estás?"])
        self.assertEqual(records[-1]["event"], "session_end")

    def test_voice_generation_and_late_interruption_are_distinct(self):
        class Engine:
            sample_rate = 1000
            executor = ThreadPoolExecutor(max_workers=1)
            tts_executor = ThreadPoolExecutor(max_workers=1)

            def load(self):
                pass

            def feed_pcm(self, pcm):
                return [SimpleNamespace(kind="partial", text="Hola", ms=0),
                        SimpleNamespace(kind="committed", text="Hola, ¿estás ahí?", ms=1000)]

            def synthesize(self, sentence):
                return b"\0\0" * 2000

            def cancel_turn(self, *args):
                pass

        class Socket:
            headers = {}

            def __init__(self):
                self.inbox = asyncio.Queue()
                self.inbox.put_nowait({"bytes": b"synthetic test audio"})
                self.replied = False

            async def accept(self):
                pass

            async def receive(self):
                return await self.inbox.get()

            async def send_json(self, message):
                if message.get("type") == "tts_end":
                    self.replied = True
                    for frame in [
                        {"type": "interrupt", "turn_id": 1, "played_ms": 100},
                        {"type": "disarm"},
                    ]:
                        await self.inbox.put({"text": json.dumps(frame)})
                elif self.replied and message.get("detail") == "muted":
                    await self.inbox.put({"type": "websocket.disconnect"})

            async def send_bytes(self, pcm):
                pass

        async def stream(*args, **kwargs):
            yield {"type": "text", "text": "Hola, estoy aquí. Podemos revisar el registro."}

        engine = Engine()
        async def run():
            await asyncio.wait_for(app.voice(Socket()), timeout=5)

        try:
            with patch.object(app, "transcript", self.log), \
                 patch.object(app.manager, "snapshot", return_value={"state": "ready"}), \
                 patch("audio_engine.get_engine", return_value=engine), \
                 patch.object(app, "stream_llm", stream):
                asyncio.run(run())
        finally:
            engine.executor.shutdown()
            engine.tts_executor.shutdown()
        records = self.records()
        generated = next(record for record in records if record["event"] == "assistant_generated")
        playback = next(record for record in records if record["event"] == "assistant_playback")
        self.assertEqual(generated["text"], "Hola, estoy aquí. Podemos revisar el registro.")
        self.assertEqual(playback["text"], "Hola, estoy aquí.")
        self.assertTrue(playback["estimated"])
        self.assertEqual(playback["status"], "interrupted")
        self.assertTrue(any(record["event"] == "asr_partial" for record in records))
        self.assertTrue(any(record["event"] == "truncated" for record in records))
        self.assertEqual(records[-1]["event"], "session_end")


if __name__ == "__main__":
    unittest.main()
