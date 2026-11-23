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

import ipaddress
import json
import math
import os
import re
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from google import genai
from google.adk.tools import ToolContext

# Directory where similar articles are stored
KNOWLEDGE_BASE_DIR = "knowledge_base"

# Cap the size of a fetched feed to avoid unbounded-memory DoS from a
# malicious/compromised feed endpoint.
MAX_FEED_BYTES = 10 * 1024 * 1024  # 10 MiB


def _is_safe_feed_url(url: str) -> bool:
    """Return True only for http(s) URLs that don't resolve to internal hosts.

    Feeds are fetched server-side, so a tampered config could otherwise be used
    to reach cloud metadata endpoints or internal services (SSRF). We require an
    http(s) scheme and reject any host that resolves to a private, loopback,
    link-local, reserved, or otherwise non-public address.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False

    try:
        addr_infos = socket.getaddrinfo(parsed.hostname, None)
    except Exception:
        return False

    for info in addr_infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _parse_feed_xml(xml_data: bytes) -> ET.Element:
    """Parse feed XML while refusing DTDs to prevent entity-expansion DoS.

    ElementTree does not resolve external entities, but internal entity
    definitions in a DTD still enable the "billion laughs" quadratic-blowup
    attack. Feeds are untrusted third-party content, so we reject any document
    that declares a DOCTYPE/DTD before parsing.
    """
    # A DTD may only appear in the prolog, before the root element. Only the
    # prolog is checked, so "<!DOCTYPE" inside article HTML (CDATA) is fine.
    prolog = _XML_PROLOG_RE.match(xml_data)
    rest = xml_data[prolog.end() if prolog else 0 :]
    if rest[:9].lower() == b"<!doctype":
        raise ValueError("XML DTD/entity declarations are not allowed in feeds")
    return ET.fromstring(xml_data)


# BOM, whitespace, XML declaration, processing instructions and comments that
# may precede a DOCTYPE or the root element.
_XML_PROLOG_RE = re.compile(rb"(?:\xef\xbb\xbf|\s+|<\?.*?\?>|<!--.*?-->)*", re.DOTALL)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target so a public URL can't bounce to an
    internal host (SSRF via redirect)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_feed_url(newurl):
            raise urllib.error.URLError("Redirect to unsafe URL blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_url_opener = urllib.request.build_opener(_SafeRedirectHandler)


def _fetch_url(url: str, timeout: int = 10) -> bytes:
    """Fetch a public http(s) URL with SSRF and size protections.

    Raises ValueError for unsafe URLs or oversized responses.
    """
    if not _is_safe_feed_url(url):
        raise ValueError("Unsafe or non-public URL")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    )
    with _url_opener.open(req, timeout=timeout) as response:
        # Read at most MAX_FEED_BYTES + 1 so we can detect overflow.
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Response exceeds size limit")
    return data


def _node_text(node: ET.Element | None) -> str:
    return node.text.strip() if node is not None and node.text else ""


def _extract_feed_items(root: ET.Element) -> list[dict]:
    """Return [{title, url, summary}] from a parsed RSS or Atom document."""
    items_found = []

    # RSS format
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            title = _node_text(item.find("title"))
            link = _node_text(item.find("link"))
            desc = _node_text(item.find("description"))
            if title and link:
                items_found.append({"title": title, "url": link, "summary": desc})
        return items_found

    # Atom format
    for entry in root.findall(".//{*}entry"):
        title = _node_text(entry.find("{*}title"))

        link = ""
        link_node = entry.find("{*}link")
        if link_node is not None:
            link = link_node.attrib.get("href", "").strip() or _node_text(link_node)

        # Note: explicit None check — an Element without children is falsy.
        summary_node = entry.find("{*}summary")
        if summary_node is None:
            summary_node = entry.find("{*}content")
        desc = _node_text(summary_node)

        if title and link:
            items_found.append({"title": title, "url": link, "summary": desc})
    return items_found


def _feed_title(root: ET.Element) -> str:
    """Return the feed's own title (RSS channel title or Atom feed title)."""
    channel = root.find("channel")
    if channel is not None:
        return _node_text(channel.find("title"))
    return _node_text(root.find("{*}title"))


def _try_parse_feed(data: bytes) -> ET.Element | None:
    """Parse data as a feed; return the root only if it is RSS or Atom."""
    try:
        root = _parse_feed_xml(data)
    except Exception:
        return None
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag in ("rss", "feed", "rdf"):
        return root
    return None


class _FeedLinkParser(HTMLParser):
    """Collect <link rel="alternate" type="application/rss+xml|atom+xml">."""

    FEED_TYPES = ("application/rss+xml", "application/atom+xml")

    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        if "alternate" in rel and a.get("type", "").lower() in self.FEED_TYPES:
            if a.get("href"):
                self.hrefs.append(a["href"])


# Common feed locations tried when a site doesn't advertise its feed.
_COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml")


def discover_feed(url: str) -> tuple[str, ET.Element]:
    """Resolve a URL to a working RSS/Atom feed.

    Accepts either a direct feed URL or a website URL. For websites, looks for
    advertised feed <link> tags, then tries a few common feed paths.
    Returns (feed_url, parsed_root). Raises ValueError if no feed is found.
    """
    data = _fetch_url(url)
    root = _try_parse_feed(data)
    if root is not None:
        return url, root

    parser = _FeedLinkParser()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
    except Exception:
        pass
    # Sites often advertise a comments feed first; try article feeds before it.
    advertised = sorted(
        (urljoin(url, href) for href in parser.hrefs),
        key=lambda u: "comments" in u.lower(),
    )
    candidates = advertised + [urljoin(url, path) for path in _COMMON_FEED_PATHS]

    seen = {url}
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            root = _try_parse_feed(_fetch_url(candidate))
        except Exception:
            continue
        # Only accept a discovered feed that actually contains articles.
        if root is not None and _extract_feed_items(root):
            return candidate, root

    raise ValueError("No RSS/Atom feed found at this URL")


def _slugify(text: str) -> str:
    """Helper to convert title into a safe filename."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return text


def _get_embedding(client: genai.Client, text: str) -> list[float]:
    """Helper to fetch embeddings using gemini-embedding-2."""
    try:
        response = client.models.embed_content(
            model="publishers/google/models/gemini-embedding-2",
            contents=text,
        )
        return response.embeddings[0].values
    except Exception as e:
        print(f"Error fetching embedding: {e}")
        # Return a zero vector as fallback
        return [0.0] * 3072


def _get_document_embedding(
    client: genai.Client, kb_dir: str, filename: str, content: str
) -> list[float]:
    """Retrieves document embedding from cache or computes it and saves it."""
    embeddings_dir = os.path.join(kb_dir, ".embeddings")
    os.makedirs(embeddings_dir, exist_ok=True)

    slug = os.path.splitext(filename)[0]
    cache_path = os.path.join(embeddings_dir, f"{slug}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                data = json.load(f)
                # Check if it has 'embedding' key and it is of expected dimension (3072)
                if "embedding" in data and len(data["embedding"]) == 3072:
                    return data["embedding"]
        except Exception:
            pass

    # Not cached or cache corrupted, compute new one
    # Use content up to 1500 chars to match previous behavior
    emb = _get_embedding(client, content[:1500])

    # Save to cache
    if emb and len(emb) == 3072:
        try:
            with open(cache_path, "w") as f:
                json.dump({"filename": filename, "embedding": emb}, f)
        except Exception as e:
            print(f"Failed to write embedding cache: {e}")

    return emb


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Helper to compute cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(v1, v2, strict=True))
    norm_v1 = math.sqrt(sum(x * x for x in v1))
    norm_v2 = math.sqrt(sum(x * x for x in v2))
    if norm_v1 == 0.0 or norm_v2 == 0.0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)


def scrape_blog_feeds(tool_context: ToolContext) -> dict:
    """Scrapes the configured tech blogs and returns new (unsummarized) articles.

    Checks the config.json for the blogs to scan, fetches their feeds, and filters
    out articles that already have matching markdown files in the knowledge base.

    Returns:
        dict: A dict containing a list of 'new_articles' (up to 3 per blog).
    """
    config_path = "config.json"
    if not os.path.exists(config_path):
        return {"error": "config.json not found", "new_articles": []}

    with open(config_path) as f:
        config = json.load(f)

    kb_dir = config.get("knowledge_base_dir", KNOWLEDGE_BASE_DIR)
    os.makedirs(kb_dir, exist_ok=True)

    # Get list of existing slugs in the knowledge base
    existing_files = os.listdir(kb_dir)
    existing_slugs = {
        os.path.splitext(f)[0] for f in existing_files if f.endswith(".md")
    }

    new_articles = []

    for blog in config.get("blogs", []):
        blog_name = blog.get("name")
        feed_url = blog.get("url")
        print(f"Scanning {blog_name} feed...")

        try:
            try:
                xml_data = _fetch_url(feed_url)
            except ValueError as e:
                print(f"Skipping {blog_name}: {e}")
                continue

            root = _parse_feed_xml(xml_data)
            items_found = _extract_feed_items(root)

            # Process up to 3 latest new articles from this feed
            count = 0
            for item in items_found:
                slug = _slugify(item["title"])
                if slug not in existing_slugs:
                    new_articles.append(
                        {
                            "blog_name": blog_name,
                            "title": item["title"],
                            "url": item["url"],
                            "summary": item["summary"],
                        }
                    )
                    count += 1
                    if count >= 3:
                        break

        except Exception as e:
            print(f"Error scanning {blog_name}: {e}")

    return {"new_articles": new_articles}


def search_similar_articles(
    title: str, summary: str, tool_context: ToolContext
) -> dict:
    """Searches the knowledge base for similar articles using Gemini embeddings.

    Args:
        title: The title of the new article.
        summary: A short description of the new article.

    Returns:
        dict: A dict containing a list of 'similar_articles' (top 3 most similar).
    """
    config_path = "config.json"
    kb_dir = KNOWLEDGE_BASE_DIR
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
            kb_dir = config.get("knowledge_base_dir", KNOWLEDGE_BASE_DIR)

    if not os.path.exists(kb_dir):
        return {"similar_articles": []}

    # Gather all saved markdown files
    kb_files = [f for f in os.listdir(kb_dir) if f.endswith(".md")]
    if not kb_files:
        return {"similar_articles": []}

    # Initialize client for embeddings
    client = genai.Client()

    # Get embedding of the query article
    query_text = f"{title}\n{summary}"
    query_vector = _get_embedding(client, query_text)

    matches = []

    for filename in kb_files:
        filepath = os.path.join(kb_dir, filename)
        try:
            with open(filepath) as f:
                content = f.read()

            # Parse title from the first line `# Title`
            lines = content.splitlines()
            file_title = ""
            for line in lines:
                if line.startswith("# "):
                    file_title = line[2:].strip()
                    break

            if not file_title:
                file_title = filename

            # Retrieve document embedding (using cache if available)
            doc_vector = _get_document_embedding(client, kb_dir, filename, content)
            score = _cosine_similarity(query_vector, doc_vector)

            # We don't want to match the article with itself if it's somehow already there
            if file_title.lower() != title.lower():
                # Store relative file path for links
                relative_path = os.path.join(kb_dir, filename)
                matches.append(
                    {
                        "title": file_title,
                        "file_path": relative_path,
                        "similarity": score,
                    }
                )
        except Exception as e:
            print(f"Error reading {filename}: {e}")

    # Sort matches by similarity score descending
    matches.sort(key=lambda x: x["similarity"], reverse=True)

    # Return top 3 matches with similarity >= 0.3 to filter out completely unrelated articles
    filtered_matches = [m for m in matches[:3] if float(m["similarity"]) >= 0.3]

    return {"similar_articles": filtered_matches}


def save_to_repository(
    title: str,
    url: str,
    extensive_summary: str,
    similar_articles: list,
    tool_context: ToolContext,
) -> dict:
    """Saves the extensive summary to a markdown file in the knowledge base.

    Args:
        title: The title of the article.
        url: The source URL of the article.
        extensive_summary: The generated extensive markdown summary.
        similar_articles: List of similar articles with 'title' and 'file_path'.

    Returns:
        dict: A dict containing the 'status' and the saved 'file_path'.
    """
    config_path = "config.json"
    kb_dir = KNOWLEDGE_BASE_DIR
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
            kb_dir = config.get("knowledge_base_dir", KNOWLEDGE_BASE_DIR)

    os.makedirs(kb_dir, exist_ok=True)
    slug = _slugify(title)
    filename = f"{slug}.md"
    filepath = os.path.join(kb_dir, filename)

    # Build markdown contents
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    markdown_content = f"# {title}\n\n"
    markdown_content += f"**Source URL**: [Link]({url})  \n"
    markdown_content += f"**Date Summarized**: {date_str}  \n\n"
    markdown_content += f"## Extensive Summary\n\n{extensive_summary}\n\n"

    markdown_content += "## Similar Articles\n\n"
    if similar_articles:
        for sim in similar_articles:
            # Create a clean relative link to the markdown file
            relative_link = os.path.basename(sim.get("file_path", ""))
            sim_title = sim.get("title", "")
            score = sim.get("similarity")
            if score is not None:
                markdown_content += f"- [{sim_title}]({relative_link}) (Similarity: {float(score):.2f})\n"
            else:
                markdown_content += f"- [{sim_title}]({relative_link})\n"
    else:
        markdown_content += "*No similar articles found in the knowledge base.*\n"

    # Save to file
    with open(filepath, "w") as f:
        f.write(markdown_content)

    return {"status": "success", "file_path": filepath}
