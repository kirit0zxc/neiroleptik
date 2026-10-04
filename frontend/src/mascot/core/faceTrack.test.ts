// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
import { faceKeyAt } from './faceTrack';
import { toStateSpec } from './presets';
import { MascotCore } from './MascotCore';
import { STATES, defineState, removeState } from './states';
import type { FaceKey, MascotRenderer } from './types';

const keys: FaceKey[] = [
  { at: 1, expression: 'surprised', weight: 0.6 },
  { at: 2.5, expression: 'happy', weight: 0.9 },
];

describe('faceKeyAt', () => {
  it('до первой метки — основное выражение', () => {
    expect(faceKeyAt(keys, 0)).toBe(-1);
    expect(faceKeyAt(keys, 0.99)).toBe(-1);
  });

  it('метка действует до следующей', () => {
    expect(faceKeyAt(keys, 1)).toBe(0);
    expect(faceKeyAt(keys, 2.4)).toBe(0);
    expect(faceKeyAt(keys, 2.5)).toBe(1);
    expect(faceKeyAt(keys, 100)).toBe(1);
  });

  it('пустая дорожка', () => {
    expect(faceKeyAt([], 5)).toBe(-1);
  });
});

describe('faceTrack в пресетах', () => {
  const base = { expression: 'neutral', weight: 0, action: 'peace', glow: {} };

  it('метки сортируются, битые отбрасываются, время округляется до сотых', () => {
    const spec = toStateSpec({
      ...base,
      faceTrack: [
        { at: 3.14159, expression: 'happy', weight: 0.8 },
        { at: 1, expression: 'smug', weight: 1 },
        { at: 'потом', expression: 'sad', weight: 1 },
        { at: 0.5, expression: 'sad', weight: 5 },
      ],
    });
    expect(spec?.faceTrack).toEqual([
      { at: 0.5, expression: 'sad', weight: 1 },
      { at: 3.14, expression: 'happy', weight: 0.8 },
    ]);
  });

  it('пустая дорожка не попадает в запись', () => {
    expect(toStateSpec({ ...base, faceTrack: [] })).not.toHaveProperty('faceTrack');
  });
});

describe('ядро передаёт дорожку рендереру', () => {
  it('вместе с выбранным клипом, а без дорожки — null', async () => {
    const setFaceTrack = vi.fn();
    const r = {
      init: async () => {}, setExpression() {}, setGaze() {}, setMouth() {},
      setVisemes() {}, setSpeaking() {}, accent() {}, blink() {}, playClip() {},
      stopClip() {}, setGlow() {}, setFaceTrack,
      capabilities: () => ({ clips: [], expressions: [] }), dispose() {},
    } as MascotRenderer;
    const core = new MascotCore({ trackPointer: false });
    await core.attach(r, document.createElement('div'));

    defineState('with_track', toStateSpec({ expression: 'neutral', weight: 0, action: 'peace', glow: {}, faceTrack: keys })!);
    core.setState('with_track');
    expect(setFaceTrack).toHaveBeenLastCalledWith('peace', keys);

    core.setState('thinking');
    expect(setFaceTrack).toHaveBeenLastCalledWith('thinking', null);

    removeState('with_track');
    core.dispose();
  });
});

describe('rootMotion', () => {
  it('ядро просит у рендерера нужную маску, а без флага — как в реестре', async () => {
    const playClip = vi.fn();
    const r = {
      init: async () => {}, setExpression() {}, setGaze() {}, setMouth() {},
      setVisemes() {}, setSpeaking() {}, accent() {}, blink() {}, playClip,
      stopClip() {}, setGlow() {},
      capabilities: () => ({ clips: [], expressions: [] }), dispose() {},
    } as MascotRenderer;
    const core = new MascotCore({ trackPointer: false });
    await core.attach(r, document.createElement('div'));

    const base = { expression: 'happy', weight: 0.5, action: 'greeting', glow: {} };
    defineState('jump', toStateSpec({ ...base, rootMotion: true })!);
    defineState('still', toStateSpec({ ...base, rootMotion: false })!);
    defineState('default', toStateSpec(base)!);

    core.setState('jump');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ mask: 'none' });
    core.setState('still');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ mask: 'inplace' });
    core.setState('default');
    expect(playClip.mock.lastCall?.[1]).not.toHaveProperty('mask');

    for (const s of ['jump', 'still', 'default']) removeState(s);
    core.dispose();
  });
});

describe('fadeMs', () => {
  it('плавность: своя у состояния, разовая из setState, иначе 280 мс', async () => {
    const playClip = vi.fn();
    const r = {
      init: async () => {}, setExpression() {}, setGaze() {}, setMouth() {},
      setVisemes() {}, setSpeaking() {}, accent() {}, blink() {}, playClip,
      stopClip() {}, setGlow() {},
      capabilities: () => ({ clips: [], expressions: [] }), dispose() {},
    } as MascotRenderer;
    const core = new MascotCore({ trackPointer: false });
    await core.attach(r, document.createElement('div'));

    defineState('slow', toStateSpec({ expression: 'happy', weight: 0.5, action: 'wave', glow: {}, fadeMs: 900 })!);
    core.setState('slow');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ fadeMs: 900 });
    core.setState('slow', { fadeMs: 120 });
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ fadeMs: 120 });
    core.setState('thinking');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ fadeMs: 280 });

    expect(toStateSpec({ expression: 'happy', weight: 0.5, glow: {}, fadeMs: 99_999 })?.fadeMs).toBe(3000);

    removeState('slow');
    core.dispose();
  });
});

describe('trim', () => {
  it('проверка данных: порядок, минимальная длина, округление', () => {
    const base = { expression: 'happy', weight: 0.5, action: 'peace', glow: {} };
    expect(toStateSpec({ ...base, trim: [2.5, 6] })?.trim).toEqual([2.5, 6]);
    expect(toStateSpec({ ...base, trim: [1.234, 3.456] })?.trim).toEqual([1.23, 3.46]);
    expect(toStateSpec({ ...base, trim: [6, 2] })).not.toHaveProperty('trim');
    expect(toStateSpec({ ...base, trim: [1, 1.01] })).not.toHaveProperty('trim');
    expect(toStateSpec({ ...base, trim: 'всё' })).not.toHaveProperty('trim');
  });

  it('ядро передаёт отрезок только для одного клипа', async () => {
    const playClip = vi.fn();
    const r = {
      init: async () => {}, setExpression() {}, setGaze() {}, setMouth() {},
      setVisemes() {}, setSpeaking() {}, accent() {}, blink() {}, playClip,
      stopClip() {}, setGlow() {},
      capabilities: () => ({ clips: [], expressions: [] }), dispose() {},
    } as MascotRenderer;
    const core = new MascotCore({ trackPointer: false });
    await core.attach(r, document.createElement('div'));

    defineState('cut', toStateSpec({ expression: 'happy', weight: 0.5, action: 'peace', trim: [2.5, 6], glow: {} })!);
    core.setState('cut');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ range: [2.5, 6] });

    defineState('cut_list', { ...STATES.cut, action: ['wave', 'nod'] });
    core.setState('cut_list');
    expect(playClip.mock.lastCall?.[1]).not.toHaveProperty('range');

    removeState('cut'); removeState('cut_list');
    core.dispose();
  });
});
