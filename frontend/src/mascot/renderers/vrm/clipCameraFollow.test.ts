// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { parsePresets } from '../../core/presets';
import { VrmRenderer } from './VrmRenderer';

/**
 * Поведение рендерера без three.js: frameCamera подменяется шпионом,
 * который лишь запоминает, каким кадром камера расставлена.
 */
type Inner = {
  currentName: string;
  bounds: unknown;
  camKey: string;
  actions: Map<string, { stop(): void; getClip(): object }>;
  frameCamera(snap?: boolean): void;
  followClipCamera(): void;
  evictVariants(keep: string): void;
};

function setup(reducedMotion: boolean) {
  const r = new VrmRenderer({ reducedMotion });
  const inner = r as unknown as Inner;
  inner.bounds = {};
  const frame = vi.spyOn(inner, 'frameCamera').mockImplementation(() => {
    inner.camKey = JSON.stringify(r.getEffectiveCamera());
  });
  return { r, inner, frame };
}

afterEach(() => {
  new VrmRenderer().setClipCamera('test-follow', null);
});

describe('переезд к кадру клипа', () => {
  it('обычно — плавно, при reduced motion — сразу', () => {
    for (const reduced of [false, true]) {
      const { r, inner, frame } = setup(reduced);
      r.setClipCamera('test-follow', { zoom: 2 });
      inner.currentName = 'test-follow';
      inner.followClipCamera();
      expect(frame).toHaveBeenLastCalledWith(reduced);
    }
  });

  it('кадр не изменился — камера не трогается', () => {
    const { inner, frame } = setup(false);
    inner.currentName = 'test-follow';
    inner.followClipCamera();
    frame.mockClear();
    inner.followClipCamera();
    expect(frame).not.toHaveBeenCalled();
  });

  it('stopClip возвращает общий кадр', () => {
    const { r, inner, frame } = setup(false);
    r.setClipCamera('test-follow', { zoom: 2 });
    inner.currentName = 'test-follow';
    inner.followClipCamera();
    frame.mockClear();
    r.stopClip();
    expect(frame).toHaveBeenCalledTimes(1);
    expect(inner.camKey).toBe(JSON.stringify(r.getCamera()));
  });

  it('forgetFile камеру не трогает: файл сразу играют заново', () => {
    const { r, inner, frame } = setup(false);
    r.setClipCamera('test-follow', { zoom: 2 });
    inner.currentName = 'test-follow';
    inner.followClipCamera();
    frame.mockClear();
    r.forgetFile('/mascot/anim/vrma/x.vrma');
    expect(frame).not.toHaveBeenCalled();
  });
});

describe('evictVariants', () => {
  it('@ в пути файла не задевает варианты соседнего клипа', () => {
    const r = new VrmRenderer();
    const inner = r as unknown as Inner;
    const fake = () => ({ stop: vi.fn(), getClip: () => ({}) });
    for (const key of ['anim/a@b.vrma@1-2', 'anim/a@b.vrma@3-4', 'anim/a@c.vrma@1-2', 'anim/a@b.vrma#legs@1-2']) {
      inner.actions.set(key, fake());
    }
    inner.evictVariants('anim/a@b.vrma@5-6');
    expect([...inner.actions.keys()]).toEqual(['anim/a@c.vrma@1-2', 'anim/a@b.vrma#legs@1-2']);
  });
});

describe('пресеты из сети', () => {
  it('ключ __proto__ через JSON.parse не становится пресетом', () => {
    const raw = JSON.parse('{"__proto__": {"expression": "happy", "weight": 1, "glow": {"color": "#fff", "intensity": 1, "pulseHz": 0}}}');
    const parsed = parsePresets(raw);
    expect(Object.keys(parsed)).toEqual([]);
    expect(Object.getPrototypeOf(parsed) === null || !('expression' in parsed)).toBe(true);
  });
});
