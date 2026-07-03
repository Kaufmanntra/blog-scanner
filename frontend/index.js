// Only allow same-origin absolute paths or http(s) URLs as link targets.
// Blocks javascript:, data:, and other dangerous schemes from untrusted
// feed/LLM content that ends up in href attributes.
function sanitizeUrl(url) {
    if (typeof url !== "string") return "#";
    const trimmed = url.trim();
    if (trimmed.startsWith("/")) return trimmed;
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    return "#";
}

// Helper to build an anchor without ever using innerHTML.
function createLink(href, text, { external = false } = {}) {
    const a = document.createElement("a");
    a.href = sanitizeUrl(href);
    a.textContent = text;
    if (external) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
    }
    return a;
}

document.addEventListener("DOMContentLoaded", () => {
    // Tab Elements
    const tabBtnScanner = document.getElementById("tab-btn-scanner");
    const tabBtnQa = document.getElementById("tab-btn-qa");
    const tabBtnSaved = document.getElementById("tab-btn-saved");
    const panelScanner = document.getElementById("panel-scanner");
    const panelQa = document.getElementById("panel-qa");
    const panelSaved = document.getElementById("panel-saved");

    // Scanner Elements
    const btnScan = document.getElementById("btn-scan");
    const blogList = document.getElementById("blog-list");
    const projectId = document.getElementById("project-id");
    const emptyState = document.getElementById("empty-state");
    const loadingState = document.getElementById("loading-state");
    const articlesGrid = document.getElementById("articles-grid");
    const statusBar = document.getElementById("status-bar");

    // Q&A Elements
    const btnAsk = document.getElementById("btn-ask");
    const chatInput = document.getElementById("chat-input");
    const chatFeed = document.getElementById("chat-feed");

    // Saved Articles Elements
    const savedEmptyState = document.getElementById("saved-empty-state");
    const savedArticlesGrid = document.getElementById("saved-articles-grid");

    // Tab Navigation Switch
    tabBtnScanner.addEventListener("click", () => {
        tabBtnScanner.classList.add("active");
        tabBtnQa.classList.remove("active");
        tabBtnSaved.classList.remove("active");
        panelScanner.classList.remove("hidden");
        panelQa.classList.add("hidden");
        panelSaved.classList.add("hidden");
    });

    tabBtnQa.addEventListener("click", () => {
        tabBtnQa.classList.add("active");
        tabBtnScanner.classList.remove("active");
        tabBtnSaved.classList.remove("active");
        panelQa.classList.remove("hidden");
        panelScanner.classList.add("hidden");
        panelSaved.classList.add("hidden");
        chatInput.focus();
    });

    tabBtnSaved.addEventListener("click", () => {
        tabBtnSaved.classList.add("active");
        tabBtnScanner.classList.remove("active");
        tabBtnQa.classList.remove("active");
        panelSaved.classList.remove("hidden");
        panelScanner.classList.add("hidden");
        panelQa.classList.add("hidden");
        loadSavedArticles();
    });

    // Initialize Page
    async function init() {
        try {
            const res = await fetch("/api/blogs");
            const data = await res.json();
            
            // Render Active Project ID
            projectId.textContent = data.project_id || "None";
            
            // Render Blogs List
            if (data.blogs && data.blogs.length > 0) {
                blogList.innerHTML = "";
                data.blogs.forEach(blog => {
                    const li = document.createElement("li");
                    li.className = "blog-item";
                    li.textContent = blog.name;
                    blogList.appendChild(li);
                });
            } else {
                blogList.innerHTML = '<li class="loading-item">No blogs configured</li>';
            }
        } catch (error) {
            console.error("Initialization error:", error);
            projectId.textContent = "Error";
            blogList.innerHTML = '<li class="loading-item">Error loading feeds</li>';
        }
    }

    // Show status bar info helper
    function showStatus(message, type = "info") {
        statusBar.className = `status-bar ${type}`;
        statusBar.querySelector(".status-message")
            ? (statusBar.querySelector(".status-message").textContent = message)
            : (statusBar.textContent = message);
        statusBar.classList.remove("hidden");
        
        // Hide status bar after 8 seconds
        setTimeout(() => {
            statusBar.classList.add("hidden");
        }, 8000);
    }

    // Scan Feeds
    btnScan.addEventListener("click", async () => {
        // Toggle Loading UI
        emptyState.classList.add("hidden");
        articlesGrid.classList.add("hidden");
        loadingState.classList.remove("hidden");
        
        btnScan.disabled = true;
        btnScan.querySelector("span").textContent = "Scanning Feeds...";
        
        try {
            const res = await fetch("/api/scan", {
                method: "POST"
            });
            const data = await res.json();
            
            loadingState.classList.add("hidden");
            
            if (data.new_articles && data.new_articles.length > 0) {
                renderArticles(data.new_articles);
                articlesGrid.classList.remove("hidden");
                showStatus(`Scan complete. Found ${data.new_articles.length} new articles.`, "success");
            } else {
                emptyState.classList.remove("hidden");
                showStatus("Scan complete. No new articles found.", "info");
            }
        } catch (error) {
            console.error("Scan error:", error);
            loadingState.classList.add("hidden");
            emptyState.classList.remove("hidden");
            showStatus("Error occurred while scanning feeds.", "error");
        } finally {
            btnScan.disabled = false;
            btnScan.querySelector("span").textContent = "Scan Feeds";
        }
    });

    // Render Articles Grid
    function renderArticles(articles) {
        articlesGrid.innerHTML = "";
        
        articles.forEach((article, index) => {
            const card = document.createElement("article");
            card.className = "article-card";
            card.id = `card-${index}`;
            
            // Header
            const header = document.createElement("div");
            header.className = "card-header";
            const blogTag = document.createElement("span");
            blogTag.className = "blog-tag";
            blogTag.textContent = article.blog_name;
            header.appendChild(blogTag);

            // Title
            const title = document.createElement("h2");
            title.appendChild(createLink(article.url, article.title, { external: true }));

            // Body (Short summary)
            const body = document.createElement("div");
            body.className = "card-body";
            const bodyP = document.createElement("p");
            bodyP.textContent = article.short_summary;
            body.appendChild(bodyP);
            
            // Footer (Actions)
            const footer = document.createElement("div");
            footer.className = "card-footer";
            
            const btnExclude = document.createElement("button");
            btnExclude.className = "btn btn-secondary btn-danger-outline";
            btnExclude.textContent = "Exclude";
            btnExclude.addEventListener("click", () => handleExclude(article, card));
            
            const btnInclude = document.createElement("button");
            btnInclude.className = "btn btn-primary btn-success";
            btnInclude.textContent = "Include";
            btnInclude.addEventListener("click", () => handleInclude(article, card, btnInclude, btnExclude));
            
            footer.appendChild(btnExclude);
            footer.appendChild(btnInclude);
            
            card.appendChild(header);
            card.appendChild(title);
            card.appendChild(body);
            card.appendChild(footer);
            
            articlesGrid.appendChild(card);
        });
    }

    // Exclude Article
    async function handleExclude(article, card) {
        try {
            const res = await fetch("/api/exclude", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: article.title })
            });
            const data = await res.json();
            
            if (data.status === "success") {
                // Add fade-out transition
                card.classList.add("removed");
                setTimeout(() => {
                    card.remove();
                    // If grid is empty, show empty state
                    if (articlesGrid.children.length === 0) {
                        articlesGrid.classList.add("hidden");
                        emptyState.classList.remove("hidden");
                    }
                }, 350);
            }
        } catch (error) {
            console.error("Exclude error:", error);
            showStatus("Failed to exclude article.", "error");
        }
    }

    // Include/Save Article
    async function handleInclude(article, card, btnInclude, btnExclude) {
        // Disable actions during save
        btnInclude.disabled = true;
        btnExclude.disabled = true;
        btnInclude.textContent = "Saving...";
        
        try {
            const res = await fetch("/api/include", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    title: article.title,
                    url: article.url,
                    summary: article.summary
                })
            });
            const data = await res.json();
            
            if (data.status === "success") {
                const footer = card.querySelector(".card-footer");
                footer.replaceChildren();

                const info = document.createElement("div");
                info.className = "save-success-info";

                const savedSpan = document.createElement("span");
                savedSpan.append("✓ Saved successfully to: ");
                const savedLink = createLink(`/${data.file_path}`, "", { external: true });
                const code = document.createElement("code");
                code.textContent = data.file_path;
                savedLink.appendChild(code);
                savedSpan.appendChild(savedLink);
                info.appendChild(savedSpan);

                // Construct similar articles list
                if (data.similar_articles && data.similar_articles.length > 0) {
                    info.appendChild(document.createElement("br"));
                    const strong = document.createElement("strong");
                    strong.textContent = "Similar articles linked:";
                    info.appendChild(strong);

                    const ul = document.createElement("ul");
                    data.similar_articles.forEach(sim => {
                        const li = document.createElement("li");
                        li.appendChild(createLink(`/${sim.file_path}`, sim.title, { external: true }));
                        li.append(` (Similarity: ${Number(sim.similarity).toFixed(2)})`);
                        ul.appendChild(li);
                    });
                    info.appendChild(ul);
                }

                footer.appendChild(info);
            } else {
                btnInclude.disabled = false;
                btnExclude.disabled = false;
                btnInclude.textContent = "Include";
                showStatus("Failed to save article.", "error");
            }
        } catch (error) {
            console.error("Save error:", error);
            btnInclude.disabled = false;
            btnExclude.disabled = false;
            btnInclude.textContent = "Include";
            showStatus("Error occurred while saving article.", "error");
        }
    }

    // Q&A Functions
    async function submitQuestion() {
        const text = chatInput.value.trim();
        if (!text) return;

        // Render User Bubble
        appendChatBubble(text, "user");
        chatInput.value = "";
        
        // Disable Controls
        chatInput.disabled = true;
        btnAsk.disabled = true;

        // Render Loading Indicator Bubble
        const loadingBubble = appendChatBubble("", "bot loading");
        loadingBubble.innerHTML = `
            <div class="dot-pulse"></div>
            <div class="dot-pulse"></div>
            <div class="dot-pulse"></div>
        `;

        try {
            const res = await fetch("/api/query", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ query: text })
            });
            const data = await res.json();
            
            // Remove loading indicator bubble
            loadingBubble.remove();

            if (res.status === 200) {
                // Render Bot Bubble with Answer and sources
                const botBubble = appendChatBubble(data.answer, "bot");
                
                if (data.sources && data.sources.length > 0) {
                    const sourcesDiv = document.createElement("div");
                    sourcesDiv.className = "sources-list";

                    const strong = document.createElement("strong");
                    strong.textContent = "Sources:";
                    sourcesDiv.appendChild(strong);

                    const ul = document.createElement("ul");
                    data.sources.forEach(src => {
                        const li = document.createElement("li");
                        li.appendChild(createLink(`/${src.file_path}`, src.title, { external: true }));
                        ul.appendChild(li);
                    });
                    sourcesDiv.appendChild(ul);

                    botBubble.appendChild(sourcesDiv);
                }
            } else {
                appendChatBubble("Sorry, I encountered an error while searching your repository: " + (data.detail || "Unknown error"), "bot");
            }
        } catch (error) {
            console.error("Q&A query error:", error);
            loadingBubble.remove();
            appendChatBubble("Sorry, a connection error occurred while querying the server.", "bot");
        } finally {
            chatInput.disabled = false;
            btnAsk.disabled = false;
            chatInput.focus();
        }
    }

    function appendChatBubble(text, className) {
        const bubble = document.createElement("div");
        bubble.className = `chat-bubble ${className}`;
        
        if (text) {
            const p = document.createElement("p");
            p.textContent = text;
            bubble.appendChild(p);
        }
        
        chatFeed.appendChild(bubble);
        chatFeed.scrollTop = chatFeed.scrollHeight;
        return bubble;
    }

    // Q&A Action Events
    btnAsk.addEventListener("click", submitQuestion);
    chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submitQuestion();
        }
    });

    // Saved Articles Functions
    async function loadSavedArticles() {
        savedArticlesGrid.innerHTML = "";
        savedEmptyState.classList.add("hidden");
        savedArticlesGrid.classList.add("hidden");

        try {
            const res = await fetch("/api/articles");
            const data = await res.json();

            if (data.articles && data.articles.length > 0) {
                renderSavedArticles(data.articles);
                savedArticlesGrid.classList.remove("hidden");
            } else {
                savedEmptyState.classList.remove("hidden");
            }
        } catch (error) {
            console.error("Failed to load saved articles:", error);
            savedEmptyState.classList.remove("hidden");
            showStatus("Failed to load saved articles from repository.", "error");
        }
    }

    function renderSavedArticles(articles) {
        articlesGrid.innerHTML = ""; // Clear main grid too just in case

        articles.forEach((art, index) => {
            const card = document.createElement("article");
            card.className = "article-card";
            card.id = `saved-card-${index}`;

            // Header
            const header = document.createElement("div");
            header.className = "card-header";
            // Check if URL belongs to a blog to extract blog label, or fallback to "CURATED"
            let label = "CURATED";
            if (art.url.includes("blog.google")) label = "Google Research";
            else if (art.url.includes("research.facebook")) label = "Meta Research";
            else if (art.url.includes("netflixtechblog")) label = "Netflix Tech";
            else if (art.url.includes("aws.amazon")) label = "AWS ML";

            const blogTag = document.createElement("span");
            blogTag.className = "blog-tag";
            blogTag.textContent = label;
            header.appendChild(blogTag);

            // Title
            const title = document.createElement("h2");
            title.appendChild(
                createLink(`/knowledge_base/${encodeURIComponent(art.filename)}`, art.title, { external: true })
            );

            // Body
            const body = document.createElement("div");
            body.className = "card-body";
            const bodyP = document.createElement("p");
            const dateStrong = document.createElement("strong");
            dateStrong.textContent = "Date Summarized:";
            bodyP.append(dateStrong, ` ${art.date || "Unknown"}`);
            bodyP.appendChild(document.createElement("br"));
            const urlStrong = document.createElement("strong");
            urlStrong.textContent = "Source URL:";
            bodyP.append(urlStrong, " ");
            bodyP.appendChild(createLink(art.url, art.url || "N/A", { external: true }));
            body.appendChild(bodyP);

            // Footer
            const footer = document.createElement("div");
            footer.className = "card-footer";

            const btnDelete = document.createElement("button");
            btnDelete.className = "btn btn-secondary btn-danger-outline";
            btnDelete.textContent = "Delete";
            btnDelete.addEventListener("click", () => handleDeleteSavedArticle(art, card));

            const btnOpen = document.createElement("a");
            btnOpen.className = "btn btn-primary";
            btnOpen.href = `/knowledge_base/${encodeURIComponent(art.filename)}`;
            btnOpen.target = "_blank";
            btnOpen.textContent = "Open Summary";

            footer.appendChild(btnDelete);
            footer.appendChild(btnOpen);

            card.appendChild(header);
            card.appendChild(title);
            card.appendChild(body);
            card.appendChild(footer);

            savedArticlesGrid.appendChild(card);
        });
    }

    async function handleDeleteSavedArticle(article, card) {
        if (!confirm(`Are you sure you want to delete the summary for "${article.title}"?`)) {
            return;
        }

        try {
            const res = await fetch(`/api/articles/${article.filename}`, {
                method: "DELETE"
            });
            const data = await res.json();

            if (data.status === "success") {
                card.classList.add("removed");
                setTimeout(() => {
                    card.remove();
                    if (savedArticlesGrid.children.length === 0) {
                        savedArticlesGrid.classList.add("hidden");
                        savedEmptyState.classList.remove("hidden");
                    }
                }, 350);
                showStatus("Article deleted successfully.", "success");
            } else {
                showStatus("Failed to delete article.", "error");
            }
        } catch (error) {
            console.error("Delete saved article error:", error);
            showStatus("Error occurred while deleting article.", "error");
        }
    }

    // Run Initialization
    init();
});
