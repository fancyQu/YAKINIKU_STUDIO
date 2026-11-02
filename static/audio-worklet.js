/* Capture original stereo PCM and a continuous 24 kHz mono recognition stream.
   No audio is played through this node; the selected tab keeps normal playback. */
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunkFrames = Math.max(1, Math.round(sampleRate / 10));
    this.ratio = sampleRate / 24000;
    this.resampleWeight = 0;
    this.resampleSum = 0;
    this.halted = false;
    this.totalFrames = 0;
    this.resetChunk();
    this.port.onmessage = (event) => {
      if (event.data?.type === "flush") {
        this.halted = true;
        this.emitChunk();
        this.port.postMessage({ type: "flushed", frames: this.totalFrames });
      }
    };
  }

  resetChunk() {
    this.original = new Int16Array(this.chunkFrames * 2);
    this.transcription = new Int16Array(Math.ceil(this.chunkFrames / this.ratio) + 4);
    this.frames = 0;
    this.monoFrames = 0;
    this.energy = 0;
  }

  pcm(value) {
    const clipped = Math.max(-1, Math.min(1, value));
    return Math.round(clipped * (clipped < 0 ? 32768 : 32767));
  }

  resample(value) {
    // Integrate source samples over target-sample intervals. Carry the fractional
    // interval across render blocks and network chunks, avoiding cumulative drift.
    let remaining = 1;
    while (remaining > 1e-9) {
      const consumed = Math.min(remaining, this.ratio - this.resampleWeight);
      this.resampleSum += value * consumed;
      this.resampleWeight += consumed;
      remaining -= consumed;
      if (this.resampleWeight >= this.ratio - 1e-9) {
        this.transcription[this.monoFrames++] = this.pcm(this.resampleSum / this.ratio);
        this.resampleWeight = 0;
        this.resampleSum = 0;
      }
    }
  }

  emitChunk() {
    if (!this.frames) return;
    const recording = this.original.buffer.slice(0, this.frames * 4);
    const transcription = this.transcription.buffer.slice(0, this.monoFrames * 2);
    this.port.postMessage({
      recording,
      transcription,
      rms: Math.sqrt(this.energy / (this.frames * 2)),
      frames: this.frames,
    }, [recording, transcription]);
    this.resetChunk();
  }

  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (this.halted) return true;
    const input = inputs[0];
    if (!input?.[0]?.length) return true;
    const left = input[0];
    const right = input[1] || left;
    for (let index = 0; index < left.length; index++) {
      const l = Number.isFinite(left[index]) ? left[index] : 0;
      const r = Number.isFinite(right[index]) ? right[index] : l;
      this.original[this.frames * 2] = this.pcm(l);
      this.original[this.frames * 2 + 1] = this.pcm(r);
      this.energy += l * l + r * r;
      this.resample((l + r) / 2);
      this.frames++;
      this.totalFrames++;
      if (this.frames === this.chunkFrames) this.emitChunk();
    }
    return true;
  }
}

registerProcessor("yakiniku-capture", CaptureProcessor);
