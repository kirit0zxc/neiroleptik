import type { Viseme, VisemeWeights } from './types';

/**
 * Липсинк по реальному звуку — для случая, когда голос приходит
 * с бэкенда отдельным аудио.
 *
 * Громкость даёт раскрытие рта, спектральный центроид — форму:
 * низкий центр тяжести спектра соответствует округлым «о/у»,
 * высокий — растянутым «и/е». Это приближение, но на глаз
 * работает заметно лучше, чем просто открывать рот по громкости.
 */
export class AudioLipSync {
  private analyser: AnalyserNode | null = null;
  private source: AudioNode | null = null;
  // Явный ArrayBuffer: Web Audio не принимает вид над SharedArrayBuffer.
  private freq = new Uint8Array(new ArrayBuffer(0));
  private time = new Uint8Array(new ArrayBuffer(0));

  attach(node: AudioNode, ctx: AudioContext) {
    this.detach();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.55;
    node.connect(analyser);
    this.source = node;
    this.analyser = analyser;
    this.freq = new Uint8Array(new ArrayBuffer(analyser.frequencyBinCount));
    this.time = new Uint8Array(new ArrayBuffer(analyser.fftSize));
  }

  /** Отключиться от звука, иначе анализаторы копятся в графе контекста. */
  detach() {
    if (this.source && this.analyser) {
      try { this.source.disconnect(this.analyser); } catch { /* уже отключён */ }
    }
    this.source = null;
    this.analyser = null;
  }

  get active() { return this.analyser !== null; }

  /** Текущие веса визем; пустой объект — тишина. */
  sample(): VisemeWeights {
    const a = this.analyser;
    if (!a) return {};

    a.getByteTimeDomainData(this.time);
    let sum = 0;
    for (let i = 0; i < this.time.length; i++) {
      const v = (this.time[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / this.time.length);
    const open = Math.min(1, Math.max(0, (rms - 0.012) * 7));
    if (open < 0.02) return {};

    a.getByteFrequencyData(this.freq);
    let weighted = 0;
    let energy = 0;
    for (let i = 0; i < this.freq.length; i++) {
      weighted += i * this.freq[i];
      energy += this.freq[i];
    }
    const centroid = energy > 0 ? weighted / energy / this.freq.length : 0.3;

    // Чем выше центр тяжести спектра, тем более «растянутая» форма рта.
    let viseme: Viseme;
    if (centroid < 0.10) viseme = 'ou';
    else if (centroid < 0.16) viseme = 'oh';
    else if (centroid < 0.24) viseme = 'aa';
    else if (centroid < 0.34) viseme = 'ee';
    else viseme = 'ih';

    return { [viseme]: open };
  }
}
