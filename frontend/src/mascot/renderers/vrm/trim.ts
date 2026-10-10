import * as THREE from 'three';

/**
 * Вырезать из клипа отрезок [start, end] секунд и сдвинуть его к нулю.
 *
 * Ключевые кадры редко попадают ровно на границы, поэтому значения на
 * границах вычисляются интерполяцией той же дорожки (для поворотов —
 * сферической). Иначе в первом кадре поза дёрнулась бы к ближайшему
 * ключу.
 */
export function trimClip(clip: THREE.AnimationClip, start: number, end: number): THREE.AnimationClip {
  const from = Math.max(0, Math.min(start, clip.duration));
  const to = Math.max(from + 1 / 60, Math.min(end, clip.duration));

  const tracks = clip.tracks.map((track) => {
    const size = track.getValueSize();
    const result = new Float32Array(size);
    const interpolant = track instanceof THREE.QuaternionKeyframeTrack
      ? new THREE.QuaternionLinearInterpolant(track.times, track.values, size, result)
      : new THREE.LinearInterpolant(track.times, track.values, size, result);
    const at = (t: number): number[] => Array.from(interpolant.evaluate(t) as Float32Array);

    const times: number[] = [0];
    const values: number[] = at(from);
    for (let i = 0; i < track.times.length; i++) {
      const t = track.times[i]!;
      if (t <= from || t >= to) continue;
      times.push(t - from);
      for (let k = 0; k < size; k++) values.push(track.values[i * size + k]!);
    }
    times.push(to - from);
    values.push(...at(to));

    const Ctor = track.constructor as new (name: string, times: number[], values: number[]) => THREE.KeyframeTrack;
    return new Ctor(track.name, times, values);
  });

  return new THREE.AnimationClip(clip.name, to - from, tracks);
}
