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
    _is_safe_feed_url,
    _parse_feed_xml,
    _slugify,
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
