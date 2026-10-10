// @vitest-environment happy-dom
import * as THREE from 'three';
import { describe, expect, it } from 'vitest';
import { trimClip } from './trim';

const clip = () => new THREE.AnimationClip('c', 2, [
  new THREE.NumberKeyframeTrack('bone.x', [0, 1, 2], [0, 10, 20]),
  new THREE.QuaternionKeyframeTrack('bone.quaternion', [0, 2], [
    0, 0, 0, 1,
    ...new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI / 2).toArray(),
  ]),
]);

describe('trimClip', () => {
  it('вырезает отрезок, сдвигает к нулю и интерполирует границы', () => {
    const t = trimClip(clip(), 0.5, 1.5);
    expect(t.duration).toBeCloseTo(1);
    const x = t.tracks[0]!;
    expect(Array.from(x.times)).toEqual([0, 0.5, 1]);
    expect(Array.from(x.values).map((v) => +v.toFixed(3))).toEqual([5, 10, 15]);
  });

  it('повороты на границах — сферически, кватернион единичный', () => {
    const q = trimClip(clip(), 1, 2).tracks[1]!;
    const start = new THREE.Quaternion().fromArray(Array.from(q.values).slice(0, 4));
    expect(start.length()).toBeCloseTo(1);
    // Середина поворота на 90° — это 45°.
    expect(2 * Math.acos(start.w)).toBeCloseTo(Math.PI / 4);
  });

  it('границы за пределами клипа зажимаются', () => {
    const t = trimClip(clip(), -3, 99);
    expect(t.duration).toBeCloseTo(2);
    expect(Array.from(t.tracks[0]!.values).map((v) => +v.toFixed(3))).toEqual([0, 10, 20]);
  });
});
