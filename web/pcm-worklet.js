// Turns the browser's float audio (usually 48 kHz) into 16 kHz mono 16-bit PCM in 100 ms chunks,
// the format the server relays to Deepgram (encoding=linear16, sample_rate=16000).
class Pcm16k extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.chunk = new Int16Array(1600);
    this.filled = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || !channels[0]) return true;
    const left = channels[0];
    const right = channels[1];
    for (let i = 0; i < left.length; i++) {
      this.sum += right ? (left[i] + right[i]) / 2 : left[i];
      this.count++;
      this.phase += 1;
      if (this.phase >= this.ratio) {
        // Average the input samples that fall into one output sample: a cheap low-pass before decimating.
        this.phase -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.sum / this.count));
        this.chunk[this.filled++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        this.sum = 0;
        this.count = 0;
        if (this.filled === this.chunk.length) {
          this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
          this.chunk = new Int16Array(1600);
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('pcm16k', Pcm16k);
