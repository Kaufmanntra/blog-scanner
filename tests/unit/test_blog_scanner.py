# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
from unittest import mock

from app.tools import (
    _cosine_similarity,
    _extract_feed_items,
    _feed_title,
    _fetch_url,
    _is_safe_feed_url,
    _parse_feed_xml,
    _slugify,
    discover_feed,
)


class TestBlogScannerUtilities(unittest.TestCase):
    def test_slugify(self) -> None:
        """Tests that titles are correctly converted to safe slug filenames."""
        self.assertEqual(_slugify("Hello World!"), "hello-world")
        self.assertEqual(
            _slugify("Google's Gemini 3.5: Advances in AI"),
            "googles-gemini-35-advances-in-ai",
        )
        self.assertEqual(_slugify("test---slug"), "test-slug")

    def test_cosine_similarity(self) -> None:
        """Tests the cosine similarity helper function."""
        # Identical vectors should have similarity of 1.0
        v1 = [1.0, 2.0, 3.0]
        v2 = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(_cosine_similarity(v1, v2), 1.0)

        # Orthogonal vectors should have similarity of 0.0
        v3 = [1.0, 0.0]
        v4 = [0.0, 1.0]
        self.assertAlmostEqual(_cosine_similarity(v3, v4), 0.0)

        # Zero vector handling
        self.assertEqual(_cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)


class TestFeedSecurity(unittest.TestCase):
    def test_parse_feed_xml_accepts_normal_feed(self) -> None:
        """A plain RSS document parses fine."""
        xml = b"<rss><channel><item><title>Hi</title></item></channel></rss>"
        root = _parse_feed_xml(xml)
        self.assertEqual(root.find("channel/item/title").text, "Hi")

    def test_parse_feed_xml_rejects_dtd(self) -> None:
        """Documents declaring a DTD/entities (billion-laughs vector) are rejected."""
        billion_laughs = (
            b'<?xml version="1.0"?>\n'
            b"<!DOCTYPE lolz [\n"
            b'  <!ENTITY lol "lol">\n'
            b'  <!ENTITY lol2 "&lol;&lol;&lol;">\n'
            b"]>\n"
            b"<rss>&lol2;</rss>"
        )
        with self.assertRaises(ValueError):
            _parse_feed_xml(billion_laughs)

    def test_parse_feed_xml_rejects_dtd_after_comment(self) -> None:
        xml = b'<?xml version="1.0"?><!-- hi --><!DOCTYPE x [<!ENTITY a "b">]><rss/>'
        with self.assertRaises(ValueError):
            _parse_feed_xml(xml)

    def test_parse_feed_xml_allows_doctype_inside_cdata(self) -> None:
        """Article HTML in CDATA may contain '<!DOCTYPE html>' — not a real DTD."""
        xml = (
            b'<?xml version="1.0"?><rss><channel><item><title>T</title>'
            b"<description><![CDATA[<!DOCTYPE html><p>x</p>]]></description>"
            b"</item></channel></rss>"
        )
        root = _parse_feed_xml(xml)
        self.assertEqual(root.find("channel/item/title").text, "T")

    def test_is_safe_feed_url_rejects_non_http_schemes(self) -> None:
        self.assertFalse(_is_safe_feed_url("file:///etc/passwd"))
        self.assertFalse(_is_safe_feed_url("ftp://example.com/feed"))
        self.assertFalse(_is_safe_feed_url("not a url"))

    def test_is_safe_feed_url_blocks_internal_hosts(self) -> None:
        """Hosts resolving to private/metadata addresses are blocked (SSRF)."""
        with mock.patch(
            "app.tools.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("169.254.169.254", 0))],
        ):
            self.assertFalse(_is_safe_feed_url("http://metadata.internal/feed"))
        with mock.patch(
            "app.tools.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 0))],
        ):
            self.assertFalse(_is_safe_feed_url("http://localhost/feed"))

    def test_is_safe_feed_url_allows_public_hosts(self) -> None:
        with mock.patch(
            "app.tools.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
        ):
            self.assertTrue(_is_safe_feed_url("https://blog.google/feed/"))


class TestFeedParsing(unittest.TestCase):
    def test_extract_rss_items(self) -> None:
        xml = (
            b"<rss><channel><title>My Blog</title>"
            b"<item><title>A</title><link>https://x.com/a</link>"
            b"<description>Desc A</description></item>"
            b"<item><title>No link</title></item>"
            b"</channel></rss>"
        )
        root = _parse_feed_xml(xml)
        self.assertEqual(
            _extract_feed_items(root),
            [{"title": "A", "url": "https://x.com/a", "summary": "Desc A"}],
        )
        self.assertEqual(_feed_title(root), "My Blog")

    def test_extract_atom_items_uses_summary(self) -> None:
        xml = (
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Blog</title>'
            b'<entry><title>B</title><link href="https://x.com/b"/>'
            b"<summary>Sum B</summary><content>Content B</content></entry>"
            b"</feed>"
        )
        root = _parse_feed_xml(xml)
        self.assertEqual(
            _extract_feed_items(root),
            [{"title": "B", "url": "https://x.com/b", "summary": "Sum B"}],
        )
        self.assertEqual(_feed_title(root), "Atom Blog")


class TestFeedDiscovery(unittest.TestCase):
    FEED = b"<rss><channel><item><title>A</title><link>https://x.com/a</link></item></channel></rss>"

    def _fake_fetch(self, pages: dict):
        def fetch(url, timeout=10):
            if url not in pages:
                raise ValueError("404")
            return pages[url]

        return fetch

    def test_direct_feed_url(self) -> None:
        pages = {"https://x.com/feed": self.FEED}
        with mock.patch("app.tools._fetch_url", self._fake_fetch(pages)):
            url, _ = discover_feed("https://x.com/feed")
        self.assertEqual(url, "https://x.com/feed")

    def test_discovers_advertised_feed_link(self) -> None:
        html = (
            b'<html><head><link rel="alternate" type="application/rss+xml" '
            b'href="/blog/rss"></head></html>'
        )
        pages = {"https://x.com/blog": html, "https://x.com/blog/rss": self.FEED}
        with mock.patch("app.tools._fetch_url", self._fake_fetch(pages)):
            url, _ = discover_feed("https://x.com/blog")
        self.assertEqual(url, "https://x.com/blog/rss")

    def test_prefers_article_feed_over_comments_feed(self) -> None:
        html = (
            b'<link rel="alternate" type="application/rss+xml" href="/comments/feed/">'
            b'<link rel="alternate" type="application/rss+xml" href="/feed/">'
        )
        empty = b"<rss><channel><title>Comments</title></channel></rss>"
        pages = {
            "https://x.com/": html,
            "https://x.com/comments/feed/": empty,
            "https://x.com/feed/": self.FEED,
        }
        with mock.patch("app.tools._fetch_url", self._fake_fetch(pages)):
            url, _ = discover_feed("https://x.com/")
        self.assertEqual(url, "https://x.com/feed/")

    def test_falls_back_to_common_paths(self) -> None:
        pages = {"https://x.com/": b"<html></html>", "https://x.com/feed": self.FEED}
        with mock.patch("app.tools._fetch_url", self._fake_fetch(pages)):
            url, _ = discover_feed("https://x.com/")
        self.assertEqual(url, "https://x.com/feed")

    def test_raises_when_no_feed(self) -> None:
        pages = {"https://x.com/": b"<html></html>"}
        with mock.patch("app.tools._fetch_url", self._fake_fetch(pages)):
            with self.assertRaises(ValueError):
                discover_feed("https://x.com/")

    def test_fetch_rejects_unsafe_url(self) -> None:
        with self.assertRaises(ValueError):
            _fetch_url("http://127.0.0.1/feed")
