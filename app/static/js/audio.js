/* Browser audio for the agent's speech. Browsers block audio until a user gesture,
 * so `unlock()` has to be called from a real click - the Start button does that. */

const OUTPUT_RATE = 24000;   // must match config.OUTPUT_SAMPLE_RATE

class Player {
  constructor() {
    this.ctx = null;
    this.node = null;
    this.ready = false;
  }

  /** Called from a click handler - creates and resumes the AudioContext. */
  async unlock() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: OUTPUT_RATE,
      });
      await this.ctx.audioWorklet.addModule("/static/worklets/playback-processor.js");
      this.node = new AudioWorkletNode(this.ctx, "playback-processor", {
        outputChannelCount: [1],
      });
      this.node.connect(this.ctx.destination);
      this.ready = true;
    }
    if (this.ctx.state !== "running") await this.ctx.resume();
    return this.ctx.state === "running";
  }

  /** Feed one PCM16 chunk straight off the WebSocket. */
  push(arrayBuffer) {
    if (!this.ready) return;
    const pcm = new Int16Array(arrayBuffer);
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    // Transfer the buffer instead of copying - this runs for every chunk.
    this.node.port.postMessage({ type: "push", samples: f32 }, [f32.buffer]);
  }

  /** Server has finished sending this turn's audio. */
  end() {
    if (this.ready) this.node.port.postMessage({ type: "end" });
  }

  /** Barge-in: drop everything not yet heard. */
  flush() {
    if (this.ready) this.node.port.postMessage({ type: "flush" });
  }

  /** Stage one of barge-in - instant, and reversible if it was just a noise. */
  pause() {
    if (this.ready) this.node.port.postMessage({ type: "pause" });
  }

  resume() {
    if (this.ready) this.node.port.postMessage({ type: "resume" });
  }

  get blocked() {
    return !this.ctx || this.ctx.state !== "running";
  }
}


/* Microphone capture. Shares the Player's AudioContext so there is only one
 * clock and no resampling between capture and playback. */
class Recorder {
  constructor() {
    this.stream = null;
    this.node = null;
    this.on = false;
  }

  async start(ctx, onChunk) {
    if (this.on) return true;

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Without echo cancellation the agent's own speech comes back through the mic and barge-in fires continuously against itself.
        echoCancellation: true,
        noiseSuppression: true,
        // OFF on purpose. AGC normalises loudness, so in a quiet room it winds the gain up and delivers chewing or a fan at speech level - destroying the very
        // level information the noise gate decides on. We do our own gating, and Deepgram copes fine with unnormalised input.
        autoGainControl: false,
      },
    });

    await ctx.audioWorklet.addModule("/static/worklets/capture-processor.js");
    this.src = ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(ctx, "capture-processor");
    this.node.port.onmessage = ({ data }) => {
      if (data.type === "calibrated") {
        if (this.onCalibrated) this.onCalibrated(data);
      } else if (data.type === "audio") {
        onChunk(data.pcm.buffer, data.level, data.open);
      }
    };
    this.src.connect(this.node);

    // A worklet only gets scheduled if its output goes somewhere. Route it
    // through a silent gain node so it runs without feeding back into the room.
    this.sink = ctx.createGain();
    this.sink.gain.value = 0;
    this.node.connect(this.sink);
    this.sink.connect(ctx.destination);

    this.on = true;
    return true;
  }

  /** Re-measure the room, e.g. if conditions change mid-session. */
  recalibrate() {
    if (this.node) this.node.port.postMessage({ type: "recalibrate" });
  }

  stop() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.src) this.src.disconnect();
    if (this.node) this.node.disconnect();
    if (this.sink) this.sink.disconnect();
    this.stream = this.node = this.src = this.sink = null;
    this.on = false;
  }
}

const player = new Player();
const recorder = new Recorder();
