/* Microphone capture in ~100ms blocks, with an adaptive noise gate: a low percentile
 * floor (not the peak), a hard cap so speech can always open it, and slow adaptation.
 * Gated blocks go out as SILENCE - Deepgram needs a stream to detect a turn ending. */

const BLOCK = 2400;             // 100ms at 24kHz
const CALIBRATION_BLOCKS = 15;  // 1.5s of listening to the room on startup
const FLOOR_PERCENTILE = 0.3;   // robust against speech during calibration
const FLOOR_MULTIPLIER = 2.5;   // speech must be this far above the floor

// The cap is the safety net. Without it, calibrating while someone talks gives a
// floor near 0.09 and a threshold near 0.27 - above speech itself - and the mic
// goes permanently deaf for the rest of the session.
// A hard band the calibrated threshold is clamped into, whatever the room measures.
//   MIN - an absolute floor. However silent the room, sound quieter than this is never
//         treated as speech. Normal talking sits around 0.08-0.25 RMS, so 0.03 is well
//         clear of typing, fans and distant voices without demanding you shout.
//   MAX - so a noisy calibration can never raise the bar above speech itself, which
//         would leave the microphone permanently deaf.
const MIN_THRESHOLD = 0.03;
const MAX_THRESHOLD = 0.05;

// Asymmetric on purpose. Symmetric tracking would let sustained background
// chatter push the floor up and shut the gate again; falling fast and rising
// slowly converges on the quietest recent level instead.
const FLOOR_FALL = 0.05;
const FLOOR_RISE = 0.002;

// Speech is sustained; a keyboard tap, a chair creak or a door is one blip. Requiring
// consecutive loud blocks costs 200ms of lead-in and rejects most impulse noise.
const OPEN_BLOCKS = 2;

const HANGOVER_BLOCKS = 8;      // ~800ms grace so word endings are not clipped
const RELEASE_RATIO = 0.6;      // close lower than we opened - no chatter

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.acc = new Float32Array(BLOCK);
    this.n = 0;
    this.muted = false;

    this.calibrating = true;
    this.samples = [];
    this.floor = MIN_THRESHOLD;
    this.threshold = MIN_THRESHOLD;

    this.open = false;
    this.hangover = 0;
    this.loud = 0;              // consecutive blocks above threshold

    this.port.onmessage = ({ data }) => {
      if (data.type === "mute") this.muted = data.value;
      else if (data.type === "recalibrate") this.startCalibration();
    };
  }

  startCalibration() {
    this.calibrating = true;
    this.samples = [];
  }

  setThreshold() {
    const t = this.floor * FLOOR_MULTIPLIER;
    this.threshold = Math.min(MAX_THRESHOLD, Math.max(MIN_THRESHOLD, t));
  }

  finishCalibration() {
    const sorted = [...this.samples].sort((a, b) => a - b);
    const idx = Math.floor(sorted.length * FLOOR_PERCENTILE);
    this.floor = sorted[idx] || MIN_THRESHOLD;
    this.calibrating = false;
    this.setThreshold();
    this.port.postMessage({
      type: "calibrated",
      floor: this.floor,
      threshold: this.threshold,
      capped: this.floor * FLOOR_MULTIPLIER > MAX_THRESHOLD,
    });
  }

  emitBlock() {
    const pcm = new Int16Array(BLOCK);

    // Root-mean-square: the honest measure of how loud this block is.
    let sum = 0;
    for (let i = 0; i < BLOCK; i++) sum += this.acc[i] * this.acc[i];
    const rms = Math.sqrt(sum / BLOCK);

    if (this.calibrating) {
      this.samples.push(rms);
      if (this.samples.length >= CALIBRATION_BLOCKS) this.finishCalibration();
      // Stay silent while calibrating so startup noise cannot barge in.
      this.port.postMessage({ type: "audio", pcm, level: rms, open: false },
                            [pcm.buffer]);
      return;
    }

    if (rms >= this.threshold) {
      // Only open once the level has HELD, so a single loud frame cannot start a turn.
      if (++this.loud >= OPEN_BLOCKS) {
        this.open = true;
        this.hangover = HANGOVER_BLOCKS;
      }
    } else if (this.open && rms >= this.threshold * RELEASE_RATIO) {
      this.loud = 0;
      this.hangover = HANGOVER_BLOCKS;      // still talking, just quieter
    } else if (this.hangover > 0) {
      this.loud = 0;
      this.hangover--;
    } else {
      this.loud = 0;
      this.open = false;
      // Only track the floor while the gate is shut, so the speaker's own voice
      // can never raise the bar against them.
      const rate = rms < this.floor ? FLOOR_FALL : FLOOR_RISE;
      this.floor += (rms - this.floor) * rate;
      this.setThreshold();
    }

    if (this.open) {
      for (let k = 0; k < BLOCK; k++) {
        const s = Math.max(-1, Math.min(1, this.acc[k]));
        pcm[k] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
    }
    // else: pcm stays zeroed - silence, so Deepgram can still endpoint.

    this.port.postMessage(
      { type: "audio", pcm, level: rms, open: this.open, threshold: this.threshold },
      [pcm.buffer],
    );
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || this.muted) return true;

    for (let i = 0; i < ch.length; i++) {
      this.acc[this.n++] = ch[i];
      if (this.n === BLOCK) {
        this.emitBlock();
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
