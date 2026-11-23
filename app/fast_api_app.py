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
import os
import re
import threading
from urllib.parse import urlparse

import google.auth
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging

from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.tools import (
    CONFIG_PATH,
    _cosine_similarity,
    _extract_feed_items,
    _feed_title,
    _get_document_embedding,
    _get_embedding,
    _slugify,
    discover_feed,
    ensure_config,
    save_to_repository,
    scrape_blog_feeds,
    search_similar_articles,
)

setup_telemetry()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ensure_config()
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=True,
)
app.title = "blog-scanner-agent"
app.description = "API for interacting with the Agent blog-scanner-agent"


# Setup the static file mount for frontend files
os.makedirs("frontend", exist_ok=True)
app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")
os.makedirs("knowledge_base", exist_ok=True)
app.mount(
    "/knowledge_base", StaticFiles(directory="knowledge_base"), name="knowledge_base"
)


def _generate_short_summary(title: str, feed_summary: str) -> str:
    """Uses Gemini 3.5 Flash to generate a clean 2-3 sentence summary."""
    client = genai.Client()
    prompt = f"""Summarize the following tech blog article in exactly 2 to 3 sentences. Keep it engaging, direct, and professional. Do not add any introductory text.
    Title: {title}
    Feed Summary: {feed_summary}
    """
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"Error generating short summary: {e}")
        # Strip HTML tags for simple fallback
        clean_desc = re.sub(r"<[^>]+>", "", feed_summary)
        return clean_desc[:200] + "..." if len(clean_desc) > 200 else clean_desc


@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard() -> FileResponse:
    """Serves the dashboard home page."""
    return FileResponse("frontend/index.html")


@app.get("/api/blogs")
def get_blogs() -> dict:
    """Returns the list of configured blogs."""
    config_path = CONFIG_PATH
    if not os.path.exists(config_path):
        return {"blogs": []}
    with open(config_path) as f:
        config = json.load(f)
    return {"blogs": config.get("blogs", []), "project_id": project_id}


# Serialises read-modify-write cycles on config.json across request threads.
_config_lock = threading.Lock()
MAX_BLOG_NAME_LENGTH = 100
MAX_BLOG_URL_LENGTH = 2048


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {"blogs": [], "knowledge_base_dir": "knowledge_base"}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _write_config(config: dict) -> None:
    """Atomically replace config.json so a crash can't leave it half-written."""
    tmp_path = f"{CONFIG_PATH}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, CONFIG_PATH)


@app.post("/api/blogs")
def add_blog(payload: dict) -> dict:
    """Adds a new source. Accepts a feed URL or a website URL (feed is auto-discovered)."""
    url = str(payload.get("url", "")).strip()
    name = str(payload.get("name", "")).strip()

    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    if len(url) > MAX_BLOG_URL_LENGTH or len(name) > MAX_BLOG_NAME_LENGTH:
        raise HTTPException(status_code=400, detail="Name or URL is too long")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"

    try:
        feed_url, root = discover_feed(url)
    except Exception as e:
        logger.log_text(f"Feed discovery failed for {url}: {e}", severity="WARNING")
        raise HTTPException(
            status_code=400,
            detail="No valid RSS/Atom feed found at this URL. Try the direct feed URL.",
        ) from e

    item_count = len(_extract_feed_items(root))
    if item_count == 0:
        raise HTTPException(status_code=400, detail="The feed contains no articles")

    if not name:
        name = (_feed_title(root) or urlparse(feed_url).hostname or feed_url)[
            :MAX_BLOG_NAME_LENGTH
        ]

    with _config_lock:
        config = _load_config()
        blogs = config.setdefault("blogs", [])
        for blog in blogs:
            if blog.get("url") == feed_url:
                raise HTTPException(
                    status_code=409, detail="This feed is already configured"
                )
            if blog.get("name", "").lower() == name.lower():
                raise HTTPException(
                    status_code=409, detail="A source with this name already exists"
                )
        new_blog = {"name": name, "url": feed_url}
        blogs.append(new_blog)
        _write_config(config)

    return {"status": "success", "blog": new_blog, "item_count": item_count}


@app.delete("/api/blogs")
def delete_blog(payload: dict) -> dict:
    """Removes a source identified by its feed URL."""
    url = str(payload.get("url", "")).strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    with _config_lock:
        config = _load_config()
        blogs = config.get("blogs", [])
        remaining = [b for b in blogs if b.get("url") != url]
        if len(remaining) == len(blogs):
            raise HTTPException(status_code=404, detail="Source not found")
        config["blogs"] = remaining
        _write_config(config)

    return {"status": "success"}


@app.post("/api/scan")
def scan_articles() -> dict:
    """Scrapes configured feeds and generates short summaries for new articles."""
    # Run feed scraping tool
    scrape_result = scrape_blog_feeds(None)
    new_articles = scrape_result.get("new_articles", [])

    # Load persistent exclusions
    excluded_path = "excluded.json"
    excluded_slugs = set()
    if os.path.exists(excluded_path):
        try:
            with open(excluded_path) as f:
                excluded_slugs = set(json.load(f))
        except Exception:
            pass

    # Filter out excluded items and generate short summaries
    filtered_articles = []
    for art in new_articles:
        slug = _slugify(art["title"])
        if slug not in excluded_slugs:
            art["short_summary"] = _generate_short_summary(art["title"], art["summary"])
            filtered_articles.append(art)

    return {"new_articles": filtered_articles}


@app.post("/api/include")
def include_article(payload: dict) -> dict:
    """Generates extensive summary, links similar articles, and saves to repository."""
    title = payload.get("title", "")
    url = payload.get("url", "")
    summary = payload.get("summary", "")

    if not title or not url:
        raise HTTPException(status_code=400, detail="Title and URL are required")

    # Generate extensive summary using Gemini 3.5 Flash
    client = genai.Client()
    prompt = f"""Generate a detailed, extensive markdown summary of the following tech blog article based on its title and original feed summary/description.
    Write a structured markdown report with sections like 'Executive Summary', 'Key Highlights/Announcements', and 'Practical Implications'. Keep it highly professional and informative.

    Title: {title}
    URL: {url}
    Feed Summary: {summary}
    """
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
        )
        extensive_summary = response.text.strip()
    except Exception as e:
        logger.log_text(f"Failed to generate summary: {e}", severity="ERROR")
        raise HTTPException(
            status_code=500, detail="Failed to generate summary"
        ) from e

    # Find similar articles
    sim_result = search_similar_articles(title, summary, None)
    similar_articles = sim_result.get("similar_articles", [])

    # Save to knowledge repository
    save_result = save_to_repository(
        title, url, extensive_summary, similar_articles, None
    )

    return {
        "status": "success",
        "file_path": save_result.get("file_path"),
        "similar_articles": similar_articles,
    }


@app.post("/api/exclude")
def exclude_article(payload: dict) -> dict:
    """Adds the article to excluded.json so it will not appear in future scans."""
    title = payload.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    slug = _slugify(title)
    excluded_path = "excluded.json"
    excluded_slugs = []

    if os.path.exists(excluded_path):
        try:
            with open(excluded_path) as f:
                excluded_slugs = json.load(f)
        except Exception:
            pass

    if slug not in excluded_slugs:
        excluded_slugs.append(slug)
        with open(excluded_path, "w") as f:
            json.dump(excluded_slugs, f, indent=2)

    return {"status": "success", "slug": slug}


@app.post("/api/query")
def query_knowledge_base(payload: dict) -> dict:
    """Performs semantic search across saved documents and answers the user's query."""
    query = payload.get("query", "")
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    config_path = CONFIG_PATH
    kb_dir = "knowledge_base"
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
                kb_dir = config.get("knowledge_base_dir", "knowledge_base")
        except Exception:
            pass

    if not os.path.exists(kb_dir):
        return {
            "answer": "No knowledge base documents found. Try scanning blogs first.",
            "sources": [],
        }

    kb_files = [f for f in os.listdir(kb_dir) if f.endswith(".md")]
    if not kb_files:
        return {
            "answer": "No knowledge base documents found. Try scanning blogs first.",
            "sources": [],
        }

    client = genai.Client()

    # Compute query embedding
    query_vector = _get_embedding(client, query)

    # Compare with all saved documents
    matches = []
    for filename in kb_files:
        filepath = os.path.join(kb_dir, filename)
        try:
            with open(filepath) as f:
                content = f.read()

            # Extract title
            file_title = ""
            for line in content.splitlines():
                if line.startswith("# "):
                    file_title = line[2:].strip()
                    break
            if not file_title:
                file_title = filename

            # Get cached or new document embedding
            doc_vector = _get_document_embedding(client, kb_dir, filename, content)
            score = _cosine_similarity(query_vector, doc_vector)

            matches.append(
                {
                    "title": file_title,
                    "file_path": f"knowledge_base/{filename}",
                    "content": content,
                    "similarity": score,
                }
            )
        except Exception as e:
            print(f"Error embedding {filename}: {e}")

    # Sort matches and take top 3 with similarity threshold > 0.15
    matches.sort(key=lambda x: x["similarity"], reverse=True)
    top_matches = [m for m in matches if float(m["similarity"]) > 0.15][:3]

    if not top_matches:
        return {
            "answer": "I couldn't find any relevant articles in the knowledge repository to answer your question.",
            "sources": [],
        }

    # Construct context for LLM
    context = ""
    for idx, match in enumerate(top_matches):
        context += f"--- Document {idx + 1}: {match['title']} ---\n"
        context += f"{match['content'][:2500]}\n\n"

    # Call Gemini to synthesize the answer
    prompt = f"""You are an assistant answering questions based ONLY on the provided articles. Synthesize a clear, concise, and helpful answer.
    Always cite the documents you used to answer (e.g. refer to them by their titles).
    If the context doesn't contain the answer, say "I don't have enough information in the repository to answer that."

    User Query: {query}

    Context documents:
    {context}
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
        )
        answer = response.text.strip()
    except Exception as e:
        logger.log_text(f"Failed to generate answer: {e}", severity="ERROR")
        raise HTTPException(
            status_code=500, detail="Failed to generate answer"
        ) from e

    sources = [{"title": m["title"], "file_path": m["file_path"]} for m in top_matches]

    return {"answer": answer, "sources": sources}


def _parse_article_metadata(filepath: str, filename: str) -> dict:
    """Helper to parse title, original URL, and summary date from markdown."""
    try:
        with open(filepath) as f:
            content = f.read()

        title = ""
        url = ""
        date = ""

        # Parse title
        for line in content.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        if not title:
            title = filename

        # Parse original URL
        url_match = re.search(r"\*\*Source URL\*\*: \[Link\]\(([^)]+)\)", content)
        if url_match:
            url = url_match.group(1).strip()

        # Parse Date Summarized
        date_match = re.search(r"\*\*Date Summarized\*\*: ([^\n]+)", content)
        if date_match:
            date = date_match.group(1).strip()

        return {"title": title, "filename": filename, "url": url, "date": date}
    except Exception as e:
        print(f"Error parsing metadata for {filename}: {e}")
        return {"title": filename, "filename": filename, "url": "", "date": ""}


@app.get("/api/articles")
def get_articles() -> dict:
    """Returns the list of articles currently saved in the knowledge repository."""
    config_path = CONFIG_PATH
    kb_dir = "knowledge_base"
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
                kb_dir = config.get("knowledge_base_dir", "knowledge_base")
        except Exception:
            pass

    if not os.path.exists(kb_dir):
        return {"articles": []}

    kb_files = [f for f in os.listdir(kb_dir) if f.endswith(".md")]

    articles = []
    for filename in kb_files:
        filepath = os.path.join(kb_dir, filename)
        articles.append(_parse_article_metadata(filepath, filename))

    return {"articles": articles}


@app.delete("/api/articles/{filename}")
def delete_article(filename: str) -> dict:
    """Deletes an article from the knowledge base and its cached embedding."""
    # Prevent directory traversal attacks
    if "/" in filename or "\\" in filename or not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    config_path = CONFIG_PATH
    kb_dir = "knowledge_base"
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
                kb_dir = config.get("knowledge_base_dir", "knowledge_base")
        except Exception:
            pass

    filepath = os.path.join(kb_dir, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Article not found")

    try:
        os.remove(filepath)
    except Exception as e:
        logger.log_text(f"Failed to delete file: {e}", severity="ERROR")
        raise HTTPException(
            status_code=500, detail="Failed to delete file"
        ) from e

    # Try deleting cached embedding
    slug = os.path.splitext(filename)[0]
    emb_path = os.path.join(kb_dir, ".embeddings", f"{slug}.json")
    if os.path.exists(emb_path):
        try:
            os.remove(emb_path)
        except Exception:
            pass

    return {"status": "success"}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
