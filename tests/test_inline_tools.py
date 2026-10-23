import json
import unittest

from tools import InlineToolParser


class ParenthesizedToolTests(unittest.TestCase):
    names = ["navigate", "browse", "browser_click", "browser_type", "browser_close",
             "emotion", "weather"]

    def parse(self, text, chunks):
        parser = InlineToolParser(self.names)
        events = []
        for chunk in chunks:
            events.extend(parser.feed(chunk))
        events.extend(parser.feed("", final=True))
        return events

    def test_calls_at_every_stream_boundary(self):
        cases = [
            ('navigate("https://www.google.com/search?q=noticias+de+hoy+madrid")', "navigate", {"url": "https://www.google.com/search?q=noticias+de+hoy+madrid"}),
            ("browse('https://example.com/a(b)?x={y}')", "browse", {"url": "https://example.com/a(b)?x={y}"}),
            ('browser_click(ref="e12")', "browser_click", {"ref": "e12"}),
            ('browser_type("e2", text="di \\"hola\\" (ahora)")', "browser_type", {"ref": "e2", "text": 'di "hola" (ahora)'}),
            ('browser_close()', "browser_close", {}),
            ('emotion("smile")', "emotion", {"name": "smile"}),
            ('weather("Valencia, España")', "weather", {"place": "Valencia, España"}),
            ("weather(place='Madrid')", "weather", {"place": "Madrid"}),
            ("weather(San Sebastián)", "weather", {"place": "San Sebastián"}),
            ("emotion(happy)", "emotion", {"name": "happy"}),
            ("navigate(https://www.reddit.com)", "navigate",
             {"url": "https://www.reddit.com"}),
            ("navigate(reddit.com)", "navigate", {"url": "reddit.com"}),
            ('navigate({"url":"https://example.com"})', "navigate", {"url": "https://example.com"}),
            ('navigate{url:<|"|>https://example.com<|"|>}', "navigate", {"url": "https://example.com"}),
        ]
        for text, name, args in cases:
            for split in range(len(text) + 1):
                with self.subTest(text=text, split=split):
                    events = self.parse(text, [text[:split], text[split:]])
                    self.assertEqual(len(events), 1, events)
                    self.assertEqual(events[0]["type"], "tool_call")
                    self.assertEqual(events[0]["name"], name)
                    self.assertEqual(json.loads(events[0]["arguments"]), args)
            self.assertEqual(len(self.parse(text, list(text))), 1)

    def test_malformed_or_incomplete_calls_never_become_speech(self):
        for text in ['navigate("https://example.com', 'navigate(url=secret())',
                     'navigate(*values)', 'browser_type("e1", "x", "extra")',
                     'navigate(url="a", url="b")']:
            with self.subTest(text=text):
                events = self.parse(text, list(text))
                self.assertFalse(any(e["type"] == "text" for e in events), events)

    def test_surrounding_speech_and_multiple_calls(self):
        text = 'Lo compruebo. navigate("https://example.com") browser_close() Listo.'
        events = self.parse(text, list(text))
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_call"],
                         ["navigate", "browser_close"])
        spoken = ''.join(e["text"] for e in events if e["type"] == "text")
        self.assertEqual(spoken, 'Lo compruebo.   Listo.')

    def test_ordinary_text_unchanged(self):
        text = 'Puedes navegar por internet. La palabra navigate no es una llamada.'
        events = self.parse(text, list(text))
        self.assertEqual(''.join(e["text"] for e in events), text)
