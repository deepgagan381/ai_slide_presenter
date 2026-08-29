/* Shared HTTP helpers. Both pages talk to the backend through here. */

const api = {
  async json(url, opts) {
    const res = await fetch(url, opts);
    let data = null;
    try {
      data = await res.json();
    }
    catch {
    }
    if (!res.ok) 
      throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
    return data;
  },

  health(provider) {
    const q = provider ? `?provider=${encodeURIComponent(provider)}` : "";
    return api.json(`/api/health${q}`);
  },
  providers() { 
    return api.json("/api/providers");
  },
  decks() {
    return api.json("/api/decks");
  },
  deck(topic) {
    return api.json(`/api/deck?topic=${encodeURIComponent(topic)}`);
  },
  generate(topic, opts = {}) {
    return api.json("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, ...opts }),
    });
  },
};

/* Elapsed-time label for long operations, so a 50s generation does not look frozen. */
function elapsed(onTick) {
  const t0 = Date.now();
  const id = setInterval(() => onTick(Math.round((Date.now() - t0) / 1000)), 500);
  return () => clearInterval(id);
}

const $ = (id) => document.getElementById(id);
