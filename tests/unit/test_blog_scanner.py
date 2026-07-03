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

from app.tools import _cosine_similarity, _slugify


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
