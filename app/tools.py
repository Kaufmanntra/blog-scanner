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

import json
import math
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from google import genai
from google.adk.tools import ToolContext

# Directory where similar articles are stored
KNOWLEDGE_BASE_DIR = "knowledge_base"


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
            req = urllib.request.Request(
                feed_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()

            root = ET.fromstring(xml_data)

            # Detect and parse items
            items_found = []

            # Check RSS format
            channel = root.find("channel")
            if channel is not None:
                for item in channel.findall("item"):
                    title_node = item.find("title")
                    link_node = item.find("link")
                    desc_node = item.find("description")

                    title = (
                        title_node.text.strip()
                        if title_node is not None and title_node.text
                        else ""
                    )
                    link = (
                        link_node.text.strip()
                        if link_node is not None and link_node.text
                        else ""
                    )
                    desc = (
                        desc_node.text.strip()
                        if desc_node is not None and desc_node.text
                        else ""
                    )

                    if title and link:
                        items_found.append(
                            {"title": title, "url": link, "summary": desc}
                        )

            # Check Atom format
            else:
                entries = root.findall(".//{*}entry")
                for entry in entries:
                    title_node = entry.find("{*}title")
                    link_node = entry.find("{*}link")
                    summary_node = entry.find("{*}summary") or entry.find("{*}content")

                    title = (
                        title_node.text.strip()
                        if title_node is not None and title_node.text
                        else ""
                    )

                    link = ""
                    if link_node is not None:
                        link = link_node.attrib.get("href", "").strip()
                        if not link:
                            link = (link_node.text or "").strip()

                    desc = (
                        summary_node.text.strip()
                        if summary_node is not None and summary_node.text
                        else ""
                    )

                    if title and link:
                        items_found.append(
                            {"title": title, "url": link, "summary": desc}
                        )

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
