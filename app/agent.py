# ruff: noqa
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

import os
import google.auth
from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.genai import types

from .tools import scrape_blog_feeds, search_similar_articles, save_to_repository
from google.adk.tools import request_input

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-3.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are an automated Blog Scanner and Knowledge Base Assistant.
Your goal is to scan configured tech blogs, summarize new articles, and help the user decide whether to save them to their knowledge repository.

You must follow this sequence exactly:
1. Call `scrape_blog_feeds` to scan the feeds for any new articles.
2. If there are no new articles, explain this to the user and stop.
3. For each new article found:
   a. Display a short summary of the article to the user (under 3 sentences).
   b. Ask the user if they want to save this article to the repository by calling the `request_input` tool with a message like: "Do you want to save '<Title>' to the repository? (yes/no)".
   c. Wait for the user's response:
      - If the user says "yes" (or approves), generate a detailed, extensive markdown summary of the article. Then, call `search_similar_articles` using the title and feed summary to find related articles already stored in the repository. Finally, call `save_to_repository` with the title, url, extensive summary, and similar articles list. Inform the user that the article was successfully saved.
      - If the user says "no" (or rejects), inform the user that the article was skipped.
4. Once all articles are processed, summarize the session and finish.
""",
    tools=[
        scrape_blog_feeds,
        search_similar_articles,
        save_to_repository,
        request_input,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
