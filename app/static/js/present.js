/* Presentation page. The deck comes from ?topic= in the URL and is handed to the
 * backend over the WebSocket. The server owns the current slide; this page only
 * renders what it is told. */

let ws = null;
let deck = null;
let current = 1;
let agentBubble = null;   // the live-growing agent message
let toastTimer = null;

const params = new URLSearchParams(location.search);
const topic = params.get("topic");
const provider = params.get("provider") || localStorage.getItem("provider") || "ollama";

/* ── boot ──────────────────────────────────────────────────────── */

async function boot() {
  if (!topic) return fail("No topic in the URL.");
  try {
    deck = await api.deck(topic);
  } catch (err) {
    return fail(err.message.includes("404") || /no deck/i.test(err.message)
      ? "That deck has not been generated yet."
      : err.message);
  }

  $("loading").hidden = true;
  $("page").hidden = false;
  document.title = `${deck.title} — AI Slide Presenter`;
  $("deck-title").textContent = deck.title;
  $("slide-total").textContent = deck.slides.length;
  buildDots();
  renderSlide(1);
  connect();
}

function fail(message) {
  $("loading").querySelector(".spinner").hidden = true;
  $("loading-text").hidden = true;
  $("loading-error").textContent = message;
  $("loading-error").hidden = false;
  $("loading-back").hidden = false;
}

/* ── rendering ─────────────────────────────────────────────────── */

function buildDots() {
  const wrap = $("dots");
  wrap.innerHTML = "";
  deck.slides.forEach((s, i) => {
    const b = document.createElement("button");
    b.title = `${i + 1}. ${s.title}`;
    b.addEventListener("click", () => send({ type: "goto", n: i + 1 }));
    wrap.appendChild(b);
  });
}

function renderSlide(n, slide) {
  current = n;
  const s = slide || deck.slides[n - 1];
  $("slide-n").textContent = n;
  $("slide-title").textContent = s.title;

  const ul = $("slide-bullets");
  ul.innerHTML = "";
  s.bullets.forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    ul.appendChild(li);
  });

  // Re-trigger the entrance animation on every change.
  const el = $("slide");
  el.style.animation = "none";
  void el.offsetHeight;
  el.style.animation = "";

  [...$("dots").children].forEach((d, i) => d.classList.toggle("on", i === n - 1));
  if (!presenting) {
    $("start").textContent = n === 1 ? "▶  Start presenting" : `▶  Present from slide ${n}`;
  }
}

function showToast(reason) {
  if (!reason) return;
  $("toast").textContent = `Jumped to slide ${current} — ${reason}`;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 5000);
}

function setBadge(state) {
  $("badge").textContent = state;
  $("badge").className = `badge ${state.toLowerCase()}`;
}

/* ── transcript ────────────────────────────────────────────────── */

function bubble(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (role !== "sys") {
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = role === "user" ? "You" : "Presenter";
    div.appendChild(who);
  }
  div.appendChild(document.createTextNode(text));
  $("transcript").appendChild(div);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  return div;
}

function appendAgent(text) {
  if (!agentBubble) agentBubble = bubble("agent", "");
  agentBubble.appendChild(document.createTextNode(text));
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

/* ── websocket ─────────────────────────────────────────────────── */

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  // Audio arrives as raw PCM16 binary frames alongside the JSON events.
  ws.binaryType = "arraybuffer";
  ws.onopen = () => send({ type: "init", deck, provider });
  ws.onclose = () => { setBadge("IDLE"); bubble("sys", "Disconnected."); };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) player.push(ev.data);
    else handle(JSON.parse(ev.data));
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function handle(ev) {
  switch (ev.type) {
    case "ready":
      voiceOn = ev.voice;
      bubble("sys", ev.voice
        ? "Ready. Press Start to hear the presentation."
        : "Ready — but no Deepgram key, so this runs silent.");
      send({ type: "audio_cache", on: audioCacheOn });
      autoListen();
      break;
    case "audio_start":
      break;
    case "audio_end":
      player.end();
      break;
    case "flush_audio":
      player.flush();
      break;
    case "state":
      setBadge(ev.value);
      if (ev.value === "SPEAKING" || ev.value === "THINKING") setPresenting(true);
      if (ev.value === "IDLE") setPresenting(false);
      if (ev.value !== "SPEAKING") agentBubble = null;
      break;
    case "slide_change":
      renderSlide(ev.n, ev.slide);
      showToast(ev.reason);
      break;
    case "transcript":
      agentBubble = null;
      bubble(ev.role, ev.text);
      break;
    case "assistant_delta":
      appendAgent(ev.text);
      break;
    case "assistant_done":
      agentBubble = null;
      break;
    case "interrupted":
      agentBubble = null;
      setBadge("INTERRUPTED");
      bubble("sys", "— interrupted —");
      break;
    case "duck":
      player.pause();          // instant, reversible
      setBadge("HEARING");
      break;
    case "unduck":
      player.resume();         // it was only noise
      setBadge("SPEAKING");
      break;
    case "partial":
      showPartial(ev.text);
      break;
    case "mic":
      micOn = ev.on;
      if (ev.on) {
        micChip(player.blocked ? "pending" : "live",
                player.blocked ? "click anywhere" : "listening");
        bubble("sys", "Listening — just talk, and interrupt any time.");
      } else if (!micMuted) {
        micChip("blocked", "mic blocked");
        bubble("sys", ev.error || "Microphone off.");
      }
      break;
    case "audio_cache": {
      cachedCount = ev.files;
      renderCacheToggle();
      // Only narrate a deliberate toggle; the passive count updates silently.
      if (ev.announce) {
        const mb = (ev.bytes / 1048576).toFixed(1);
        bubble("sys", ev.on
          ? `Reusing stored audio — ${ev.files} sentences (${mb} MB).`
          : "Reuse off — every sentence re-synthesised, and still stored.");
      }
      break;
    }
    case "resume_available":
      bubble("sys", `Holding your place — slide ${ev.slide}.`);
      break;
    case "resuming":
      bubble("sys", `Picking up from slide ${ev.slide}…`);
      break;
    case "stopped":
      setPresenting(false);
      bubble("sys", "Stopped.");
      break;
    case "presentation_complete":
      setPresenting(false);
      bubble("sys", "Presentation finished. Ask me anything.");
      break;
    case "error":
      bubble("sys", `Error: ${ev.message}`);
      break;
  }
}

/* ── live speech feedback ──────────────────────────────────────── */

let micOn = false;
let micMuted = false;

/** While you speak, the ask box shows what the STT is hearing in real time.
 *  It is the clearest possible signal that the microphone is actually working. */
function showPartial(text) {
  const box = $("ask");
  if (text) {
    box.value = text;
    box.classList.add("hearing");
  } else {
    box.value = "";
    box.classList.remove("hearing");
  }
}

/** Draw the input level. Full scale at 0.3 RMS covers normal speech.
 *  Runs ~10x/second, so it must never throw if the markup is absent. */
function showLevel(level, open) {
  const fill = $("meter-fill");
  if (fill) fill.style.width = `${Math.min(100, (level / 0.3) * 100)}%`;
  $("mic").classList.toggle("speaking-over", !!open);
}

function micChip(state, label) {
  $("mic").className = `mic-chip ${state}`;
  $("mic-label").textContent = label;
}

/** Start listening unprompted. Browsers will not run an AudioContext until the page
 * is interacted with, so this can get the microphone and still be suspended - hence
 * the one-shot listener that goes live on the first click. */
async function autoListen() {
  if (micMuted) return;
  micChip("pending", "starting mic…");
  try {
    await player.unlock();
    recorder.onCalibrated = ({ floor, threshold }) => {
      bubble("sys", `Room measured — ignoring anything below ${threshold.toFixed(3)} `
                  + `(background is ${floor.toFixed(3)}).`);
    };
    await recorder.start(player.ctx, (pcm, level, open) => {
      if (ws && ws.readyState === WebSocket.OPEN && !micMuted) ws.send(pcm);
      showLevel(level, open);
    });
    send({ type: "mic_on" });

    if (player.blocked) {
      micChip("pending", "click anywhere");
      armGesture();
    }
  } catch (err) {
    micOn = false;
    micChip("blocked", "mic blocked");
    bubble("sys", err.name === "NotAllowedError"
      ? "Microphone permission denied — you can still type your questions."
      : `Microphone unavailable: ${err.message}. Typing still works.`);
  }
}

function armGesture() {
  const go = async () => {
    document.removeEventListener("pointerdown", go);
    document.removeEventListener("keydown", go);
    await player.unlock();
    if (!player.blocked && !micMuted) micChip("live", "listening");
  };
  document.addEventListener("pointerdown", go);
  document.addEventListener("keydown", go);
}

function toggleMute() {
  micMuted = !micMuted;
  if (micMuted) {
    recorder.stop();
    send({ type: "mic_off" });
    showPartial("");
    micChip("muted", "muted");
  } else {
    autoListen();
  }
}

/* ── controls ──────────────────────────────────────────────────── */

let voiceOn = false;

/** Browsers block audio until a user gesture, so every control that can make
 *  the agent speak unlocks the context first. */
async function withAudio(fn) {
  try { await player.unlock(); } catch (err) { console.warn("audio unlock failed", err); }
  fn();
}

let presenting = false;

function setPresenting(on) {
  presenting = on;
  $("start").textContent = on
    ? "■  Stop presenting"
    : (current === 1 ? "▶  Start presenting" : `▶  Present from slide ${current}`);
  $("start").classList.toggle("stopping", on);
}

$("start").addEventListener("click", () => {
  if (presenting) {
    player.flush();
    send({ type: "stop" });          // full stop - will not auto-resume
  } else {
    withAudio(() => send({ type: "start", from: current }));
  }
});
$("mic").addEventListener("click", toggleMute);

// Persisted, and switchable mid-session: flip it between slides to compare
// cached playback against fresh synthesis.
let audioCacheOn = localStorage.getItem("audioCache") !== "off";

let cachedCount = 0;

/** A SETTING, not a status: "reuse stored audio when it exists". Saying "Cached audio: on"
 *  on a deck with nothing stored read as a contradiction, so the count is shown too. */
function renderCacheToggle() {
  const b = $("cache");
  b.className = `ghost cache ${audioCacheOn ? "on" : "off"}`;
  b.textContent = audioCacheOn
    ? `♻︎ Reuse audio: on · ${cachedCount || "none yet"}`
    : "♻︎ Reuse audio: off";
  b.title = audioCacheOn
    ? `${cachedCount} sentences stored for this deck; new ones are added as they play`
    : "Every sentence re-synthesised. New audio is still stored.";
}

$("cache").addEventListener("click", () => {
  audioCacheOn = !audioCacheOn;
  localStorage.setItem("audioCache", audioCacheOn ? "on" : "off");
  renderCacheToggle();
  send({ type: "audio_cache", on: audioCacheOn });
});
renderCacheToggle();
$("stop").addEventListener("click", () => { player.flush(); send({ type: "interrupt" }); });
$("prev").addEventListener("click", () => send({ type: "goto", n: Math.max(1, current - 1) }));
$("next").addEventListener("click", () =>
  send({ type: "goto", n: Math.min(deck.slides.length, current + 1) }));

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("ask").value.trim();
  if (!text) return;
  withAudio(() => send({ type: "user_text", text }));
  $("ask").value = "";
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("stop").click(); return; }   // always available
  if (document.activeElement === $("ask")) return;
  if (e.key === "ArrowRight") $("next").click();
  if (e.key === "ArrowLeft") $("prev").click();
});

boot();
