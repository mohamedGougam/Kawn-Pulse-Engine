const $ = (id) => document.getElementById(id);

const els = {
  query: $("query"),
  searchBtn: $("searchBtn"),
  refreshBtn: $("refreshBtn"),
  status: $("status"),
  summary: $("summary"),
  summaryText: $("summaryText"),
  sentLabel: $("sentLabel"),
  sentScore: $("sentScore"),
  barPos: $("barPos"),
  barNeu: $("barNeu"),
  barNeg: $("barNeg"),
  posPct: $("posPct"),
  neuPct: $("neuPct"),
  negPct: $("negPct"),
  themes: $("themes"),
  sources: $("sources"),
  cards: $("cards"),
  topicId: $("topicId"),
  cardsHeader: $("cardsHeader"),
};

let currentTopicId = null;
let currentQuery = null;

function setStatus(msg, isError = false) {
  els.status.classList.remove("hidden");
  els.status.textContent = msg;
  els.status.style.borderColor = isError ? "rgba(255,69,58,.45)" : "rgba(255,255,255,.10)";
}

function clearStatus() {
  els.status.classList.add("hidden");
  els.status.textContent = "";
}

function pct(x) {
  const v = Math.max(0, Math.min(1, Number(x) || 0));
  return Math.round(v * 100);
}

function pillClass(label) {
  if (label === "positive") return "pos";
  if (label === "negative") return "neg";
  return "neu";
}

function renderThemes(themes) {
  els.themes.innerHTML = "";
  (themes || []).forEach((t) => {
    const div = document.createElement("div");
    div.className = "tag";
    div.textContent = t;
    els.themes.appendChild(div);
  });
}

function renderSources(rows) {
  els.sources.innerHTML = "";
  (rows || []).sort((a, b) => (b.item_count || 0) - (a.item_count || 0)).forEach((r) => {
    const div = document.createElement("div");
    div.className = "sourceRow";
    div.innerHTML = `<span><strong>${escapeHtml(r.source)}</strong></span><span>${r.item_count} items • ${r.card_count} cards</span>`;
    els.sources.appendChild(div);
  });
}

function actionLabel(source) {
  const s = String(source || "").toLowerCase();
  if (s === "youtube") return "Watch on YouTube";
  if (s === "reddit") return "Open Reddit thread";
  if (s === "news") return "Read article";
  if (s === "bluesky") return "Open on Bluesky";
  if (s === "hackernews") return "Open on Hacker News";
  return "Open source";
}

function hostFromUrl(url) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch (_) {
    return "";
  }
}

function renderCards(cards) {
  els.cards.innerHTML = "";
  (cards || []).forEach((c) => {
    const card = document.createElement("article");
    card.className = "card clickable";
    card.setAttribute("data-href", c.source_url || "");
    const sentCls = pillClass(c.sentiment);
    const theme = c.theme ? `<span class="pill">${escapeHtml(c.theme)}</span>` : "";
    const engagement = (c.engagement_count ?? null) !== null ? ` • ${c.engagement_count}` : "";
    const host = hostFromUrl(c.source_url || "");
    const action = actionLabel(c.source);
    card.innerHTML = `
      <div class="quote">“${escapeHtml(c.quote)}”</div>
      <div class="meta">
        <div class="metaTags">
          <span class="pill ${sentCls}">${escapeHtml(c.source)} • ${escapeHtml(c.sentiment)}${engagement}</span>
          ${theme}
          ${host ? `<span class="pill host">${escapeHtml(host)}</span>` : ""}
        </div>
        <a class="action" href="${escapeAttr(c.source_url)}" target="_blank" rel="noreferrer">
          ${escapeHtml(action)} <span class="arrow">↗</span>
        </a>
      </div>
    `;
    els.cards.appendChild(card);
  });

  els.cards.querySelectorAll(".card.clickable").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      const href = el.getAttribute("data-href");
      if (href) window.open(href, "_blank", "noopener,noreferrer");
    });
  });
}

function renderSummary(resp) {
  els.summary.classList.remove("hidden");
  els.cards.classList.remove("hidden");
  els.cardsHeader.classList.remove("hidden");
  els.refreshBtn.classList.remove("hidden");

  const s = resp.summary;
  els.summaryText.textContent = s.summary_text || "(No summary yet)";
  els.sentLabel.textContent = s.sentiment_label || "neutral";
  els.sentScore.textContent = `score: ${Number(s.sentiment_score || 0).toFixed(3)}`;

  const b = s.sentiment_breakdown || {};
  const p = pct(b.positive), n = pct(b.neutral), g = pct(b.negative);

  els.barPos.style.width = `${p}%`;
  els.barNeu.style.width = `${n}%`;
  els.barNeg.style.width = `${g}%`;

  els.posPct.textContent = `${p}%`;
  els.neuPct.textContent = `${n}%`;
  els.negPct.textContent = `${g}%`;

  renderThemes(s.themes || []);
  renderSources(resp.source_breakdown || []);
  renderCards(resp.cards || []);

  currentTopicId = resp.topic?.id || null;
  currentQuery = resp.topic?.query || null;
  els.topicId.textContent = currentTopicId ? `topic_id: ${currentTopicId}` : "";
}

async function apiSearch(query) {
  const r = await fetch("/api/topics/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`Search failed (${r.status}): ${t}`);
  }
  return await r.json();
}

async function apiRefresh(topicId) {
  const r = await fetch(`/api/topics/${encodeURIComponent(topicId)}/refresh`, { method: "POST" });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`Refresh failed (${r.status}): ${t}`);
  }
  return await r.json();
}

function setBusy(busy) {
  els.searchBtn.disabled = busy;
  els.refreshBtn.disabled = busy;
  els.searchBtn.textContent = busy ? "Working…" : "Search Pulse";
}

async function onSearch() {
  const q = (els.query.value || "").trim();
  if (!q) return;
  clearStatus();
  setBusy(true);
  try {
    setStatus("Fetching sources, cleaning, running AI, and building pulse cards…");
    const resp = await apiSearch(q);
    renderSummary(resp);
    setStatus(`Done. Showing pulse for “${q}”.`);
  } catch (e) {
    setStatus(String(e?.message || e), true);
  } finally {
    setBusy(false);
  }
}

async function onRefresh() {
  if (!currentTopicId || !currentQuery) return;
  clearStatus();
  setBusy(true);
  try {
    setStatus("Refreshing topic…");
    await apiRefresh(currentTopicId);
    const resp = await apiSearch(currentQuery);
    renderSummary(resp);
    setStatus("Refresh complete.");
  } catch (e) {
    setStatus(String(e?.message || e), true);
  } finally {
    setBusy(false);
  }
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(s) {
  // For href only; keep it simple.
  return String(s ?? "").replaceAll('"', "%22");
}

els.searchBtn.addEventListener("click", onSearch);
els.refreshBtn.addEventListener("click", onRefresh);
els.query.addEventListener("keydown", (e) => {
  if (e.key === "Enter") onSearch();
});

