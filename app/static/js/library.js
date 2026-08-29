/* Landing page. One selection drives one action button, whose job is to always be
 * the obviously-right next step: Start if the deck exists, Generate if it does not. */

const SUGGESTIONS = [
  "How AI voice agents work",
  "The history of the espresso machine",
  "How solar storms affect the power grid",
  "Why sourdough bread rises",
];

let decks = [];          // cached decks from the server
let selected = null;     // { topic, exists }
let busy = false;
// The landing page does not scroll, so only the most recent decks are shown.
// Typing filters them, which is how you reach the rest.
const MAX_VISIBLE_DECKS = 10;
let providers = [];      // from /api/providers
let provider = localStorage.getItem("provider") || "ollama";

/* ── selection ─────────────────────────────────────────────────── */

function select(topic, exists) {
  selected = topic ? { topic, exists } : null;
  render();
}

/** A typed topic may already exist in the library — match case-insensitively so
 *  retyping a known topic offers Start rather than a pointless regeneration. */
function findDeck(topic) {
  const t = topic.trim().toLowerCase();
  return decks.find((d) => d.topic.trim().toLowerCase() === t) || null;
}

/* ── rendering ─────────────────────────────────────────────────── */

function visibleDecks() {
  const typed = $("topic").value.trim().toLowerCase();
  const matches = typed
    ? decks.filter(
      (d) => d.title.toLowerCase().includes(typed) || d.topic.toLowerCase().includes(typed)
    ) : decks;
  return matches.slice(0, MAX_VISIBLE_DECKS);
}

function renderCards() {
  const wrap = $("cards");
  wrap.querySelectorAll(".card").forEach((c) => c.remove());

  const shown = visibleDecks();
  $("empty").hidden = decks.length > 0;
  const hidden = decks.length - shown.length;
  $("deck-count").textContent = decks.length
    ? `${decks.length} deck${decks.length === 1 ? "" : "s"}` +
      (hidden > 0 ? ` · ${hidden} more, keep typing to filter` : "")
    : "";

  shown.forEach((d, i) => {
    const card = document.createElement("button");
    card.className = "card";
    card.style.animationDelay = `${Math.min(i * 35, 300)}ms`;
    card.classList.toggle("sel", !!selected && selected.topic === d.topic);
    card.innerHTML = `
      <span class="c-title"></span>
      <span class="c-meta">${d.slide_count} slides · ready</span>`;
    card.querySelector(".c-title").textContent = d.title;
    card.title = d.topic;
    card.addEventListener("click", () => {
      $("topic").value = "";
      select(d.topic, true);
    });
    wrap.appendChild(card);
  });
}

function render() {
  renderCards();

  const btn = $("action");
  const label = $("selected");

  if (busy) return; // generating - the button is owned by generate()

  if (!selected) {
    label.innerHTML = `<span class="muted">Nothing selected</span>`;
    btn.textContent = "Select a topic";
    btn.disabled = true;
    return;
  }

  label.innerHTML = `<span class="label">Selected</span><span class="val"></span>`;
  label.querySelector(".val").textContent = selected.topic;
  btn.disabled = false;
  btn.textContent = selected.exists ? "▶  Start presentation" : "✨  Generate deck";

  // Decks are cached by topic, so switching provider would otherwise silently
  // hand back the deck the previous model wrote. Regenerate forces a rewrite.
  $("regen").hidden = !selected.exists;
}

/** Provider choice. Persisted per browser so it survives a reload, and sent with
 *  both the generate request and the presentation session. */
function renderProviders() {
  const wrap = $("providers");
  wrap.innerHTML = "";
  providers.forEach((p) => {
    const b = document.createElement("button");
    b.className = "provider" + (p.id === provider ? " sel" : "");
    b.disabled = !p.ready;
    b.title = p.ready ? p.model : p.hint;
    b.innerHTML = `<span class="p-name"></span><span class="p-sub"></span>`;
    b.querySelector(".p-name").textContent = p.label;
    // A provider without a key says so, rather than failing on first use.
    b.querySelector(".p-sub").textContent = p.ready ? p.model : p.hint;
    b.addEventListener("click", () => {
      provider = p.id;
      localStorage.setItem("provider", provider);
      renderProviders();
      pollHealth();
    });
    wrap.appendChild(b);
  });
}

/* ── actions ───────────────────────────────────────────────────── */

function goPresent(topic) {
  location.href =
    `/present?topic=${encodeURIComponent(topic)}&provider=${encodeURIComponent(provider)}`;
}

async function generate(topic, force = false) {
  busy = true;
  $("error").hidden = true;
  const btn = $("action");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span><span>Writing your deck…</span>`;
  const labelEl = btn.querySelector("span:last-child");

  const stop = elapsed((s) => {
    labelEl.textContent = `Writing your deck… ${s}s`;
  });

  try {
    await api.generate(topic, { provider, force });
    goPresent(topic);            // straight into the presentation once it exists
  } catch (err) {
    $("error").textContent = err.message;
    $("error").hidden = false;
    await refresh();
  } finally {
    stop();
    busy = false;
    render();
  }
}

$("regen").addEventListener("click", () => {
  if (selected && !busy) generate(selected.topic, true);
});

$("action").addEventListener("click", () => {
  if (!selected || busy) return;
  if (selected.exists) goPresent(selected.topic);
  else generate(selected.topic);
});

/* ── composer ──────────────────────────────────────────────────── */

$("topic").addEventListener("input", (e) => {
  const val = e.target.value.trim();
  // select() re-renders, which re-applies the filter to the deck grid.
  if (!val) return select(null);
  const hit = findDeck(val);
  select(hit ? hit.topic : val, !!hit);
});

$("topic").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && selected && !busy) {
    e.preventDefault();
    $("action").click();
  }
});

/* ── boot ──────────────────────────────────────────────────────── */

function renderSuggestions() {
  const wrap = $("suggest");
  SUGGESTIONS.filter((s) => !findDeck(s)).forEach((s) => {
    const b = document.createElement("button");
    b.textContent = s;
    b.addEventListener("click", () => {
      $("topic").value = s;
      select(s, false);
      $("topic").focus();
    });
    wrap.appendChild(b);
  });
}

async function refresh() {
  try {
    const list = await api.providers();
    providers = list.providers || [];
    // A remembered choice whose key has since gone falls back to the default.
    if (!providers.some((p) => p.id === provider && p.ready)) provider = list.default;
    renderProviders();
  } catch { /* selector stays empty; Ollama is still the default server-side */ }

  try {
    decks = (await api.decks()).decks || [];
  } catch {
    decks = [];
  }
  $("suggest").innerHTML = "";
  renderSuggestions();
  render();
}

/* Ollama may still be booting when the page loads - the app starts it. Poll
 * until it settles so the user never sees a red dot that is about to go green. */

/** Quantisation suffixes make a name long enough to swallow the whole line, so
 *  "qwen2.5:3b-instruct-q4_K_M" reads as one model rather than one of two.
 *  The full name stays in the tooltip. */
const shortModel = (name) => name.replace(/(:[^-]+)-.*$/, "$1");

const HEALTH_LABELS = {
  // Both models get named and labelled by role. One missing is otherwise invisible
  // until it fails mid-demo — generation on the first deck, chat on the first question.
  ready: (h) => {
    const missing = h.missing || [];
    if (missing.length) {
      return `Missing ${missing.map(shortModel).join(" and ")} — run: ollama pull ${missing[0]}`;
    }
    const roles = { generation: "deck", chat: "answers" };
    const parts = (h.required || []).map(
      (m) => `${shortModel(m.name)} for ${roles[m.role] || m.role}`);
    return `Ollama ready · ${parts.join(" · ")}`;
  },
  starting: () => "Starting Ollama…",
  unknown:  () => "Checking Ollama…",
  missing:  () => "Ollama is not installed",
  down:     () => "Ollama would not start — see logs/ollama.log",
};

async function pollHealth(attempt = 0) {
  let h;
  try {
    h = await api.health(provider);
  } catch {
    $("health-dot").className = "dot off";
    $("health-text").textContent = "backend unreachable";
    return;
  }

  const state = h.state || (h.ollama ? "ready" : "down");
  // Hosted providers have no local models to check - a working key is the whole story.
  const label = h.hosted
    ? `${h.label} ready · ${h.model}`
    : (HEALTH_LABELS[state] || HEALTH_LABELS.down)(h);

  $("health-dot").className = `dot ${h.ollama ? "on" : h.settling ? "" : "off"}`;
  $("health-text").textContent = label;
  $("health-text").title = (h.required || [])
    .map((m) => `${m.role}: ${m.name} ${m.present ? "installed" : "MISSING"}`)
    .join("\n");
  $("health-text").classList.toggle("error", !h.ollama && !h.settling);

  // A pulled-but-missing model is an error even though Ollama itself is up.
  if (h.ollama && (h.missing || []).length) $("health-text").classList.add("error");

  // Keep checking for ~60s while it is still coming up.
  if (h.settling && attempt < 30) setTimeout(() => pollHealth(attempt + 1), 2000);
}

// Both run on load: refresh() draws the deck library,
// pollHealth() reports on Ollama.
refresh();
pollHealth();
