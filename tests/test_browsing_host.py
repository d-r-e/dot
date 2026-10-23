import unittest

from app import browsing_host


class BrowsingHostTests(unittest.TestCase):
    def test_only_hostname_is_extracted(self):
        cases = {
            "https://www.aemet.es/es/eltiempo/prediccion/municipios/madrid-id28079": "aemet.es",
            "https://EXAMPLE.com:8443/path?q=1#x": "example.com",
            "http://sub.example.co.uk/": "sub.example.co.uk",
            "https://example.com./": "example.com",
            "https://[2001:db8::1]/": "2001:db8::1",
        }
        for url, host in cases.items():
            with self.subTest(url=url):
                self.assertEqual(browsing_host("navigate", {"url": url}), host)
                self.assertEqual(browsing_host("browse", '{"url": "%s"}' % url), host)

    def test_only_navigation_tools(self):
        url = {"url": "https://example.com/"}
        for name in ("browser_click", "browser_type", "weather", "emotion", ""):
            with self.subTest(name=name):
                self.assertIsNone(browsing_host(name, url))

    def test_malformed_calls_have_no_label(self):
        for arguments in ("", "not json", '{"url": 3}', '{"url": null}', "[]", None,
                          '{"url": "https://exa mple.com/"}',
                          '{"url": "https://exämple.com/"}'):
            with self.subTest(arguments=arguments):
                self.assertIsNone(browsing_host("navigate", arguments))

    def test_label_is_bounded(self):
        long_url = "https://" + "a" * 300 + ".example/"
        self.assertIsNone(browsing_host("navigate", {"url": long_url}))


if __name__ == "__main__":
    unittest.main()
