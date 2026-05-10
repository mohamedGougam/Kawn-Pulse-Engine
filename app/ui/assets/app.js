const $ = (id) => document.getElementById(id);

const els = {
  query: $("query"),
  language: $("language"),
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
  langFilter: $("langFilter"),
  langChips: $("langChips"),
};

const LANG_LABELS = {
  en: "English",
  fr: "French",
  ar: "Arabic",
  es: "Spanish",
  de: "German",
  pt: "Portuguese",
  it: "Italian",
  ru: "Russian",
  he: "Hebrew",
  hi: "Hindi",
  el: "Greek",
  ja: "Japanese",
  ko: "Korean",
  zh: "Chinese",
  th: "Thai",
  und: "Unknown",
};

function langLabel(code) {
  if (!code) return LANG_LABELS.und;
  return LANG_LABELS[code.toLowerCase()] || code.toUpperCase();
}

let currentTopicId = null;
let currentQuery = null;
let currentCards = [];
let currentSourceFilter = null;
let currentLanguageFilter = null;

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

  const allRow = document.createElement("button");
  allRow.type = "button";
  allRow.className = "sourceRow filter" + (currentSourceFilter === null ? " active" : "");
  allRow.dataset.source = "__all__";
  const totalItems = (rows || []).reduce((acc, r) => acc + (r.item_count || 0), 0);
  const totalCards = (rows || []).reduce((acc, r) => acc + (r.card_count || 0), 0);
  allRow.innerHTML = `<span><strong>All sources</strong></span><span>${totalItems} items • ${totalCards} cards</span>`;
  els.sources.appendChild(allRow);

  (rows || []).sort((a, b) => (b.card_count || 0) - (a.card_count || 0)).forEach((r) => {
    const btn = document.createElement("button");
    btn.type = "button";
    const isActive = currentSourceFilter && currentSourceFilter.toLowerCase() === String(r.source).toLowerCase();
    btn.className = "sourceRow filter" + (isActive ? " active" : "") + ((r.card_count || 0) === 0 ? " disabled" : "");
    btn.dataset.source = r.source;
    btn.innerHTML = `<span><strong>${escapeHtml(r.source)}</strong></span><span>${r.item_count} items • ${r.card_count} cards</span>`;
    els.sources.appendChild(btn);
  });

  els.sources.querySelectorAll("button.sourceRow").forEach((btn) => {
    btn.addEventListener("click", () => {
      const src = btn.dataset.source;
      if (src === "__all__") {
        currentSourceFilter = null;
      } else if (currentSourceFilter && currentSourceFilter.toLowerCase() === src.toLowerCase()) {
        currentSourceFilter = null;
      } else {
        currentSourceFilter = src;
      }
      applyCardFilter();
      // Update active state without re-fetching breakdown.
      els.sources.querySelectorAll("button.sourceRow").forEach((b) => {
        const s = b.dataset.source;
        const active = (currentSourceFilter === null && s === "__all__") ||
                       (currentSourceFilter && s.toLowerCase() === currentSourceFilter.toLowerCase());
        b.classList.toggle("active", !!active);
      });
    });
  });
}

function applyCardFilter() {
  let filtered = currentCards;
  if (currentSourceFilter) {
    filtered = filtered.filter((c) => String(c.source).toLowerCase() === currentSourceFilter.toLowerCase());
  }
  if (currentLanguageFilter) {
    filtered = filtered.filter((c) => String(c.language || "und").toLowerCase() === currentLanguageFilter.toLowerCase());
  }
  renderCards(filtered);
}

function renderLanguageChips() {
  const counts = new Map();
  currentCards.forEach((c) => {
    const k = String(c.language || "und").toLowerCase();
    counts.set(k, (counts.get(k) || 0) + 1);
  });

  if (counts.size === 0) {
    els.langFilter.classList.add("hidden");
    els.langChips.innerHTML = "";
    return;
  }
  els.langFilter.classList.remove("hidden");

  els.langChips.innerHTML = "";

  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "chip" + (currentLanguageFilter === null ? " active" : "");
  allBtn.dataset.lang = "__all__";
  allBtn.textContent = `All (${currentCards.length})`;
  els.langChips.appendChild(allBtn);

  Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .forEach(([code, count]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const isActive = currentLanguageFilter && currentLanguageFilter.toLowerCase() === code;
      btn.className = "chip" + (isActive ? " active" : "");
      btn.dataset.lang = code;
      btn.textContent = `${langLabel(code)} (${count})`;
      els.langChips.appendChild(btn);
    });

  els.langChips.querySelectorAll("button.chip").forEach((b) => {
    b.addEventListener("click", () => {
      const lang = b.dataset.lang;
      if (lang === "__all__") {
        currentLanguageFilter = null;
      } else if (currentLanguageFilter && currentLanguageFilter.toLowerCase() === lang) {
        currentLanguageFilter = null;
      } else {
        currentLanguageFilter = lang;
      }
      applyCardFilter();
      els.langChips.querySelectorAll("button.chip").forEach((x) => {
        const c = x.dataset.lang;
        const active = (currentLanguageFilter === null && c === "__all__") ||
                       (currentLanguageFilter && c === currentLanguageFilter.toLowerCase());
        x.classList.toggle("active", !!active);
      });
    });
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
    const langPill = c.language
      ? `<span class="pill lang">${escapeHtml(langLabel(c.language))}</span>`
      : "";
    card.innerHTML = `
      <div class="quote">“${escapeHtml(c.quote)}”</div>
      <div class="meta">
        <div class="metaTags">
          <span class="pill ${sentCls}">${escapeHtml(c.source)} • ${escapeHtml(c.sentiment)}${engagement}</span>
          ${theme}
          ${langPill}
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

  currentCards = resp.cards || [];
  currentSourceFilter = null;
  currentLanguageFilter = null;
  renderSources(resp.source_breakdown || []);
  renderLanguageChips();
  applyCardFilter();

  currentTopicId = resp.topic?.id || null;
  currentQuery = resp.topic?.query || null;
  els.topicId.textContent = currentTopicId ? `topic_id: ${currentTopicId}` : "";
}

async function apiSearch(query, language) {
  const body = { query };
  if (language) body.language = language;
  const r = await fetch("/api/topics/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
  const lang = (els.language?.value || "").trim();
  clearStatus();
  setBusy(true);
  try {
    setStatus(
      lang
        ? `Searching "${q}" in ${langLabel(lang)}…`
        : "Fetching sources, cleaning, running AI, and building pulse cards…"
    );
    const resp = await apiSearch(q, lang || null);
    renderSummary(resp);
    if (lang) {
      currentLanguageFilter = lang;
      renderLanguageChips();
      applyCardFilter();
    }
    setStatus(`Done. Showing pulse for "${q}"${lang ? ` (${langLabel(lang)})` : ""}.`);
  } catch (e) {
    setStatus(String(e?.message || e), true);
  } finally {
    setBusy(false);
  }
}

async function onRefresh() {
  if (!currentTopicId || !currentQuery) return;
  const lang = (els.language?.value || "").trim();
  clearStatus();
  setBusy(true);
  try {
    setStatus("Refreshing topic…");
    await apiRefresh(currentTopicId);
    const resp = await apiSearch(currentQuery, lang || null);
    renderSummary(resp);
    if (lang) {
      currentLanguageFilter = lang;
      renderLanguageChips();
      applyCardFilter();
    }
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

