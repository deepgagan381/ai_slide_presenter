/* Playback ring buffer. A ring rather than queued AudioBufferSourceNodes because
 * barge-in must drop unplayed audio *instantly*, and scheduled nodes cannot be
 * cleanly recalled once started. Zeroing two indices can. */

const CAPACITY = 24000 * 90;   // 90s at 24kHz - far more than any single turn

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(CAPACITY);
    this.read = 0;
    this.write = 0;
    this.draining = false;   // true once the server says the turn is complete

    // Ducking is the first half of barge-in: VAD fires, we pause instantly, and
    // only commit to a full flush once real words confirm it. A cough resumes.
    this.paused = false;

    this.port.onmessage = ({ data }) => {
      if (data.type === "push") this.push(data.samples);
      else if (data.type === "flush") this.flush();
      else if (data.type === "end") this.draining = true;
      else if (data.type === "pause") this.paused = true;
      else if (data.type === "resume") this.paused = false;
    };
  }

  push(samples) {
    this.draining = false;
    for (let i = 0; i < samples.length; i++) {
      this.buf[this.write % CAPACITY] = samples[i];
      this.write++;
    }
    // Overflow means we fell more than 90s behind; drop the oldest audio rather
    // than let read and write cross.
    if (this.write - this.read > CAPACITY) this.read = this.write - CAPACITY;
  }

  flush() {
    this.read = 0;
    this.write = 0;
    this.draining = false;
    this.paused = false;
    this.port.postMessage({ type: "flushed" });
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;

    // Paused: emit silence but do NOT advance `read`, so resuming continues
    // exactly where it stopped rather than skipping ahead.
    if (this.paused) {
      out.fill(0);
      return true;
    }

    let played = 0;
    for (let i = 0; i < out.length; i++) {
      if (this.read < this.write) {
        out[i] = this.buf[this.read % CAPACITY];
        this.read++;
        played++;
      } else {
        out[i] = 0;   // underrun - silence rather than a click
      }
    }

    if (played > 0) {
      this.elapsed = (this.elapsed || 0) + played;
      // Report roughly 10x/sec so the page can show real playback progress.
      if (!this.lastReport || this.elapsed - this.lastReport > 2400) {
        this.lastReport = this.elapsed;
        this.port.postMessage({ type: "progress", samples: this.elapsed });
      }
    } else if (this.draining && this.read >= this.write) {
      this.draining = false;
      this.port.postMessage({ type: "idle" });
    }

    return true;
  }
}

registerProcessor("playback-processor", PlaybackProcessor);
