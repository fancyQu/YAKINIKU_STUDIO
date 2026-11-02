// Runs the real AudioWorklet processor in a deterministic Node VM without a
// browser, microphone, cloud API, or third-party test dependency.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "audio-worklet.js"), "utf8");

for (const rate of [96000, 88200, 48000, 44100, 32000, 24000]) {
  let Processor;
  class Base {
    constructor() {
      this.messages = [];
      this.port = { onmessage: null, postMessage: (message) => this.messages.push(message) };
    }
  }
  vm.runInNewContext(source, {
    AudioWorkletProcessor: Base,
    sampleRate: rate,
    registerProcessor: (name, processor) => { Processor = processor; },
    Int16Array, Math, Number,
  });
  const processor = new Processor();
  const frameCount = rate * 2 + 137;
  for (let offset = 0; offset < frameCount; offset += 128) {
    const size = Math.min(128, frameCount - offset);
    const left = Float32Array.from({ length: size }, () => .25);
    const right = Float32Array.from({ length: size }, () => -.125);
    const output = new Float32Array(size).fill(1);
    processor.process([[left, right]], [[output]]);
    assert.ok(output.every((value) => value === 0), "capture must not replay the selected tab");
  }
  processor.port.onmessage({ data: { type: "flush" } });
  let originalFrames = 0;
  let monoFrames = 0;
  for (const message of processor.messages.filter((item) => item.recording)) {
    originalFrames += message.recording.byteLength / 4;
    monoFrames += message.transcription.byteLength / 2;
    const stereo = new Int16Array(message.recording);
    for (let index = 0; index < stereo.length; index += 2) {
      assert.equal(stereo[index], 8192);
      assert.equal(stereo[index + 1], -4096);
    }
    const mono = new Int16Array(message.transcription);
    assert.ok(mono.every((value) => Math.abs(value - 2048) <= 1), "mono must average the stereo channels");
    assert.ok(Math.abs(message.rms - Math.sqrt((.25 ** 2 + .125 ** 2) / 2)) < 1e-6);
  }
  assert.equal(originalFrames, frameCount, "recording must retain the final incomplete network chunk");
  assert.equal(monoFrames, Math.floor(frameCount * 24000 / rate + 1e-8), "resampling must not accumulate block-boundary drift");
  assert.equal(processor.messages.at(-1).type, "flushed");
  const messagesBefore = processor.messages.length;
  processor.process([[new Float32Array(128)]], [[new Float32Array(128)]]);
  assert.equal(processor.messages.length, messagesBefore, "flush must halt subsequent capture");
  console.log(`${rate} Hz: original stereo, 24 kHz mono, flush, and silent output pass`);
}

console.log("AudioWorklet tests passed.");
