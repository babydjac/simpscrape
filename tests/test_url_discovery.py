import unittest

from core.url_discovery import discover_urls


class UrlDiscoveryTests(unittest.TestCase):
    def test_decodes_socialmediagirls_link_confirmation_urls(self) -> None:
        records = [
            {
                "links": [
                    "https://forums.socialmediagirls.com/goto/link-confirmation"
                    "?url=aHR0cHM6Ly9idW5rci5zaS9mL2FiY2QxMjM0"
                ],
                "images": [],
                "videos": [],
                "iframes": [],
                "embeds": [],
                "attachments": [],
                "quotes": [],
                "post_content": "",
                "post_id": "post-1",
                "post_author": "tester",
                "post_date": "2026-03-15T00:00:00+00:00",
                "web_scraper_start_url": "https://forums.socialmediagirls.com/threads/whorella.379890/",
            }
        ]

        result = discover_urls(records)

        self.assertEqual(result.unique_urls, ["https://bunkr.si/f/abcd1234"])
        self.assertEqual(result.host_urls, {"bunkr.si": ["https://bunkr.si/f/abcd1234"]})

    def test_keeps_existing_redirect_to_support(self) -> None:
        records = [
            {
                "links": ["https://forum.example/redirect?to=https%3A%2F%2Fexample.com%2Ffile.mp4"],
                "images": [],
                "videos": [],
                "iframes": [],
                "embeds": [],
                "attachments": [],
                "quotes": [],
                "post_content": "",
                "post_id": "post-2",
                "post_author": "tester",
                "post_date": "2026-03-15T00:00:00+00:00",
                "web_scraper_start_url": "https://forum.example/threads/demo.1/",
            }
        ]

        result = discover_urls(records)

        self.assertEqual(result.unique_urls, ["https://example.com/file.mp4"])

    def test_deep_mode_scans_title_field(self) -> None:
        records = [
            {
                "links": [],
                "images": [],
                "videos": [],
                "iframes": [],
                "embeds": [],
                "attachments": [],
                "quotes": [],
                "post_content": "",
                "title": "See https://extra.example/deep-file.zip for more",
                "post_id": "post-3",
                "post_author": "tester",
                "post_date": "2026-03-15T00:00:00+00:00",
                "web_scraper_start_url": "https://forum.example/threads/demo.2/",
            }
        ]
        shallow = discover_urls(records, deep=False)
        self.assertEqual(shallow.unique_urls, [])

        deep = discover_urls(records, deep=True)
        self.assertEqual(deep.unique_urls, ["https://extra.example/deep-file.zip"])


if __name__ == "__main__":
    unittest.main()
