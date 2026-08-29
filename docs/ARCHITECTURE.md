# Architecture

How the app is put together: the flow through it, the request paths, and why each tuning
constant is the value it is.

The README covers what it does and how to run it. This is the level below that.

---

## The user journey

```text
                          ┌───────────────────┐
                          │   LANDING  ( / )  │
                          │  decks · model ·  │
                          │    topic box      │
                          └─────────┬─────────┘
                                    │
                   ┌────────────────┴────────────────┐
             deck already exists?              new topic
                   │                                 │
                   │                     ┌───────────▼───────────┐
                   │                     │  GENERATE             │
                   │                     │  LLM writes 6 slides: │
                   │                     │  title · bullets ·    │
                   │                     │  spoken notes ·       │
                   │                     │  routing keywords     │
                   │                     │  ~50s local, ~5s host │
                   │                     └───────────┬───────────┘
                   │                                 │ saved to disk
                   └────────────────┬────────────────┘
                                    ▼
                    ┌───────────────────────────────┐
                    │  PRESENTATION ( /present )    │
                    │  server owns the slide;       │
                    │  browser only renders         │
                    └───────────────┬───────────────┘
                                    │
        ┌───────────────┬───────────┴───────┬──────────────────┐
        ▼               ▼                   ▼                  ▼
   ▶ Start         you SPEAK           you ask a          ‹ Library
   narrates the    over it             question           (leave)
   deck aloud      → barge-in          → routes to the
   slide by slide  → it stops            right slide,
        │            in ~0.7s            answers, then
        │               │                hands back
        └───────────────┴────────────────────┘
                        │
                        ▼
              ■ Stop  → IDLE, forgets where it was
```

The landing page never presents anything. Its whole job is to make sure a deck exists for
your topic, then hand off.

---

## Session states

Four states. Every arrow is something the listener did.

```text
                    press Start / ask a question
        ┌──────┐   ─────────────────────────────▶  ┌──────────┐
        │ IDLE │                                    │ SPEAKING │◀────┐
        └──────┘  ◀─────────  ■ Stop  ─────────────└─────┬────┘     │
            ▲       (clears the resume point)             │          │
            │                                    you speak│          │
            │                                             ▼          │
            │                                     ┌──────────────┐   │
            │                                     │   HEARING    │   │
            │                                     │ (playback    │   │
            │                                     │  PAUSED)     │   │
            │                                     └──┬────────┬──┘   │
            │                            real words  │        │  no words
            │                             → COMMIT   ▼        ▼   in 1.5s
            │                            ┌───────────┐   ┌─────────┐
            │                            │ LISTENING │   │ resume  │
            │                            └─────┬─────┘   │ playing │
            │                       question   │         └────┬────┘
            │                        finished  ▼              │
            │                            ┌──────────┐         │
            └────────────────────────────│ THINKING │─────────┘
                     error               └────┬─────┘
                                              │ answer ready
                                              └────────────────▶ SPEAKING
```

`HEARING` is the two-stage barge-in. Deepgram's VAD fires roughly 0.7s before the first word
is transcribed, so playback **pauses** on the VAD signal and only **commits** once real words
confirm it. A cough resumes playback rather than derailing the talk.

---

## How a question is handled

```text
                        question arrives (voice or typed)
                                      │
                                      ▼
                    ┌─────────────────────────────────────┐
                    │  ONE schema-constrained LLM call    │
                    │  → slide_number, relevance,         │
                    │    reason, say   (all REQUIRED)     │
                    └──────────────────┬──────────────────┘
                                       │
                     ┌─────────────────┼─────────────────┐
                 in_deck            related          unrelated
                     │                 │                 │
                     │                 │      does the question use the
                     │                 │      deck's own vocabulary?
                     │                 │            │         │
                     │                 │◀──── yes ──┘        no
                     │                 │   (demote — refusing        │
                     │                 │    a fair question is worse)│
                     ▼                 ▼                             ▼
            answer from the    "The slides don't          "That's outside what
            chosen slide        cover this. From           I'm presenting today."
                     │          what I know…"              model's text DISCARDED
                     │                 │                   deck does NOT move
                     └────────┬────────┘                             │
                              ▼                                      │
                   did the slide change?                             │
                     │                  │                            │
                    yes                 no                           │
                     │                  │                            │
                     ▼                  │                            │
        already narrated?               │                            │
         │             │                │                            │
        yes            no               │                            │
         ▼             ▼                │                            │
   "We covered    "We'll be             │                            │
    this on 2."    covering this        │                            │
                   on slide 3."         │                            │
         └──────┬────────┘              │                            │
                └───────────┬───────────┴────────────────────────────┘
                            ▼
                  was a talk underway?
                     │            │
                    yes           no
                     │            │
                     ▼            ▼
        "Anyway, let's get     stay where we are
         back to where          (just browsing)
         we were."
         → resumes AT the
           interrupted sentence
```

Routing is a **required schema field**, not a tool call. A 3B model asked to call
`goto_slide` mid-conversation degrades into *narrating* the intent — "Let's go to slide 3" —
without emitting the call, so the deck never moves. A required field cannot be skipped.

The same technique carries `relevance`, and a word-overlap guard demotes `unrelated` back to
`related` when the question uses the deck's own vocabulary. The guard only ever loosens:
wrongly refusing a fair question is worse than answering a stray one.

---

## The audio pipeline

```text
  YOU SPEAK                                        IT SPEAKS
  ─────────                                        ─────────
  microphone                                    ┌──────────────┐
      │                                         │  a sentence  │
      ▼                                         └──────┬───────┘
  echo cancellation                                    │
  (browser)                                    cached on disk?
      │                                          │          │
      ▼                                        yes          no
  capture worklet                                │          │
   ├ 100ms blocks                                │          ▼
   ├ noise gate:                                 │   Deepgram Aura-2
   │   below floor → send SILENCE                │   (~3× realtime)
   │   above floor → send AUDIO                  │          │
   └ (never drops: Deepgram needs                │          ▼
      a continuous stream to                     │    store on COMPLETE
      detect a turn ending)                      │      streams only
      │                                          │          │
      ▼                                          └────┬─────┘
  Deepgram Nova-3                                     ▼
   ├ SpeechStarted  → pause playback          ring buffer in the browser
   ├ partial        → live text + COMMIT       ├ pause  = freeze read index
   └ final          → answer it                └ flush  = zero both indices
                                                        │
                                                        ▼
                                                    speakers
```

Slides are paced by **audio duration**, never by how long synthesis took. Deepgram produces
speech about three times faster than realtime, so returning when the bytes were sent would
rip through six slides in seconds. `bytes / 48000` is the true playback length, and sleeping
it out is also a clean cancellation point for barge-in.

Playback is a ring buffer rather than a queue of scheduled audio nodes, because barge-in must
drop unplayed audio instantly and scheduled nodes cannot be cleanly recalled once started.

---

## Three sockets

The browser holds one connection. The server holds two more.

```text
  BROWSER                     SERVER                        DEEPGRAM

              ┌──── socket 1 ────┐
   mic ═══════╪═══ binary up ════╪══► ws_session ══ socket 2 ══► STT
              │                  │
   speakers ◄═╪══ binary down ═══╪══  ws_session ◄═ socket 3 ══  TTS
              │                  │
   UI      ◄══╪═══ JSON both ════╪══► session state
              └──────────────────┘
```

On socket 1, direction determines meaning: binary going up is always microphone audio, binary
coming down is always speech. The WebSocket protocol distinguishes text frames from binary
ones, so no envelope is needed.

The browser never talks to Deepgram directly. That keeps the API key on the server, and it is
what lets the server sit in the middle and make decisions — gating noise, judging whether a
transcript is worth interrupting for, pacing slides against audio.

---

## Request flows

### Startup

```text
uvicorn app.main:app
└─ core/logging_setup.py:setup_logging()          console + rotating files
└─ main.py:lifespan()
   └─ asyncio.create_task(warmup())            ← BACKGROUND, does not block startup
   │  └─ ensure_running()
   │     ├─ Ollama already up? → READY, _proc stays None
   │     ├─ binary missing?    → MISSING, logs where it looked
   │     └─ otherwise          → spawn `ollama serve` detached, poll to 45s
   │  └─ warn per unpulled model, then send one token to load it into RAM
   └─ yield                                     ← accepting requests
   (on shutdown)
   └─ stop_ollama()   — only if _proc is set, i.e. only if we started it
```

### Landing page — `GET /`

```text
main.py:page_library()  →  static/index.html

library.js:
├─ refresh()
│  ├─ GET /api/providers  → three entries, `ready` = key present
│  ├─ GET /api/decks      → glob .deck_cache/*/deck.json, newest first
│  └─ renderCards()       → filter by typed text, cap at 10
└─ pollHealth()
   └─ GET /api/health?provider=…
      ├─ hosted?  → ready immediately, a working key is the whole story
      └─ ollama?  → /api/tags, then check BOTH required models
      └─ settling → re-poll every 2s while Ollama is still starting
```

### Generating a deck — `POST /api/generate`

```text
main.py:api_generate(payload)
├─ resolve_provider(provider)     no key → falls back to "ollama"
├─ make_llm(provider) → (client, gen_model, chat_model)
└─ generate_deck(topic, …, use_cache=not force)
   ├─ load_cached(topic)                 HIT → return, ~26ms
   ├─ rmtree(deck_dir/"audio")           regenerating invalidates the speech too
   └─ escalating attempts:
      │  1. (gen_model, first try)
      │  2. (gen_model, retry WITH the validation error fed back)
      │  3. (chat_model, last resort)
      ├─ llm.complete_json(msgs, deck_json_schema(), model)
      │  ├─ Ollama       → {"format": <schema>}
      │  └─ OpenAI-style → response_format json_schema (see to_strict_schema)
      ├─ Deck.from_dict()   exactly N slides · non-empty title · ≥1 bullet ·
      │                     notes ≥40 chars · slides renumbered 1..N
      ├─ save_cached(deck)
      └─ llm.unload(gen_model)           free RAM before the chat model is needed
```

### Opening a presentation — `GET /present?topic=…&provider=…`

```text
present.js:boot()
├─ GET /api/deck?topic=…      404 → "That deck has not been generated yet."
├─ renderSlide(1), buildDots()
└─ connect()  →  ws.onopen → {type:"init", deck, provider}

main.py:ws_session()   one coroutine per connection
├─ DeepgramTTS().start()        opened NOW — the handshake costs ~1.3s
├─ DeepgramSTT(on_speech)       NOT connected; waits for mic_on
└─ loop: frame = await ws.receive()
   ├─ frame["bytes"] → stt.send_audio(…)
   └─ frame["text"]  → dispatch on `kind`

on "init":
├─ Deck.from_dict(…)            re-validated, never trusted
├─ PresentationSession(deck, emit, llm, tts, emit_audio, chat_model)
└─ emit "ready"  →  client starts the microphone
```

### Presenting

```text
session.py:present_all(start, from_sentence)
└─ for each slide:  goto(n) → _narrate(n)
   └─ _narrate: speaker_notes → _say(…), NOT recorded in history

session.py:_say(text, base_index)
├─ create_task(_stream_audio(sentences))          ← BACKGROUND
├─ for each sentence:                             ← FOREGROUND
│  ├─ _sentence_index = base_index + i            the resume position
│  ├─ emit assistant_delta
│  └─ sleep(len(sentence) / CHARS_PER_SECOND)     ← cancellation point
└─ sleep(duration_of(total_bytes) − elapsed)      ← cancellation point

session.py:_stream_audio(sentences)
└─ per sentence:  cache hit → replay in 16KB frames
                  miss     → synthesise, play, store on COMPLETE streams only
```

Narration is deliberately **not** appended to conversation history. Measured: with six slides
of narration in context, the same question routed to slide 2 instead of slide 3 — the model
believed it had already covered everything.

### Interruption

```text
deepgram_stt.py normalises Deepgram's wire format into three events:
   speech_started · partial · final

main.py:on_speech(ev)
├─ "speech_started" while SPEAKING
│  └─ emit "duck"  → playback pauses; arm a 1.5s resume timer
├─ "partial"
│  ├─ emit for the live ask box
│  └─ ≥5 chars AND confidence ≥0.5?  → cancel timer, session.interrupt()
│     otherwise → log and ignore; the timer decides
└─ "final" → session.answer(text)

session.py:interrupt()
├─ capture slide + sentence BEFORE cancelling
├─ _task.cancel()
├─ tts.clear()          only if a stream is genuinely in flight
├─ emit flush_audio     zero the browser's ring buffer
├─ record the resume point
└─ history += what was ACTUALLY spoken
```

Cancelling on the server stops nothing you can hear — synthesis runs ahead of playback, so
the audio is already in the browser. Both ends have to be cleared.

### Answering

```text
session.py:answer(question)
├─ messages = [presenter_prompt(deck, current, narrated)] + history[-6:]
│  └─ the prompt carries the FULL deck: bullets and notes, not just titles
├─ llm.complete_json(…, ANSWER_SCHEMA)
├─ relevance branch (see "How a question is handled")
├─ goto(target) unless unrelated
├─ _say(answer)
└─ _resume() if a talk was interrupted
   └─ static bridging line, then present_all(from the interrupted sentence)
```

The presenter prompt puts the deck first and the volatile state (current slide, slides
narrated) last, so Ollama can reuse its cached prefix rather than reprocessing ~1,100 tokens
on every question.

---

## The numbers that matter

Six values shape how the app behaves. The rest are ordinary tuning, documented in the code
beside what they affect.

| Value | Why |
|---|---|
| **24000 Hz → 48000 bytes/s** | `linear16` is two bytes per sample, so `bytes / 48000` is exactly how long audio takes to play. That is what paces the slides. |
| **16 chars/sec** | Measured speech rate. The transcript is revealed in step with the audio, which is what makes the resume position truthful rather than estimated. |
| **0.03 – 0.05 RMS** | The band the noise gate is clamped into. The floor is absolute, because a quiet room makes the calibration meaningless; the ceiling keeps the threshold below speech so the microphone can never end up deaf. |
| **30th percentile** | The room is measured by percentile, not by peak — otherwise one loud moment during calibration poisons the floor. |
| **1.5 seconds** | How long playback stays paused waiting for words to justify it. Must exceed the ~0.7s lag before the first word is transcribed. |
| **5 chars · 0.5 confidence** | What it takes to actually cancel a sentence. Rejects a stray "uh" and words the transcriber invented from noise. |

Two measurements sit behind several of these: Deepgram synthesises about **3× faster than
realtime**, and its voice detector fires about **0.7s before the first word** is transcribed.

### Automatic gain control is off

The microphone requests echo cancellation and noise suppression, but not automatic gain
control. AGC evens out loudness — and the noise gate decides *by* loudness, so AGC erases the
signal it depends on.
