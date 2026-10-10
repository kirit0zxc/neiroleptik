// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MascotCore } from './MascotCore';
import type { MascotRenderer } from './types';
import type { TTSHandlers, TTSProvider } from '../voice/types';
import { DANCES } from './clips';

/** Рендерер-заглушка: считает вызовы и умеет «грузиться» сколько скажут. */
function fakeRenderer(initMs = 0) {
  const calls = { clips: [] as string[], blinks: 0, disposed: 0, expressions: [] as string[] };
  const r: MascotRenderer = {
    init: () => new Promise((res) => setTimeout(res, initMs)),
    setExpression: (name) => { calls.expressions.push(name); },
    setGaze: () => {},
    setMouth: () => {},
    setVisemes: () => {},
    setSpeaking: () => {},
    accent: () => {},
    blink: () => { calls.blinks++; },
    playClip: (name) => { calls.clips.push(name); },
    stopClip: () => {},
    setGlow: () => {},
    capabilities: () => ({ clips: [], expressions: [] }),
    dispose: () => { calls.disposed++; },
  };
  return { r, calls };
}

/** Голос, который «говорит» ровно столько, сколько задано. */
function fakeVoice(ms: number): TTSProvider {
  return {
    name: 'fake',
    hasBoundaries: false,
    speak: (_text: string, h: TTSHandlers) => new Promise((res) => {
      h.onStart?.();
      setTimeout(() => { h.onEnd?.(); res(); }, ms);
    }),
    cancel: () => {},
  };
}

const host = () => document.createElement('div');

describe('MascotCore', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('применяет состояние только к загруженному рендереру', async () => {
    const core = new MascotCore({ trackPointer: false });
    const { r, calls } = fakeRenderer(100);
    const attached = core.attach(r, host());
    core.setState('thinking');
    expect(calls.clips).toEqual([]);
    await vi.advanceTimersByTimeAsync(100);
    await attached;
    expect(calls.clips).toEqual(['thinking']);
    expect(core.getRenderer()).toBe(r);
  });

  it('временное состояние само возвращается в idle', async () => {
    const core = new MascotCore({ trackPointer: false });
    const { r } = fakeRenderer();
    const attached = core.attach(r, host());
    await vi.advanceTimersByTimeAsync(0);
    await attached;
    core.setState('welcome');
    await vi.advanceTimersByTimeAsync(3300);
    expect(core.getState()).toBe('idle');
  });

  it('двойной монтаж (StrictMode): живёт один рендерер, моргание не удваивается', async () => {
    const core = new MascotCore({ trackPointer: false, blinkRange: [1000, 1000] });
    const first = fakeRenderer(50);
    const second = fakeRenderer(50);

    const a = core.attach(first.r, host());
    core.detach(first.r);                       // размонтирование
    const b = core.attach(second.r, host());    // второй монтаж
    await vi.advanceTimersByTimeAsync(60);
    await Promise.all([a, b]);

    expect(core.getRenderer()).toBe(second.r);
    expect(first.calls.disposed).toBeGreaterThan(0);
    expect(second.calls.disposed).toBe(0);

    // Двойное моргание иногда добавляет второе — убираем случайность.
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    await vi.advanceTimersByTimeAsync(3000);
    expect(second.calls.blinks).toBe(3);
  });

  it('detach старого рендерера не снимает новый', async () => {
    const core = new MascotCore({ trackPointer: false });
    const first = fakeRenderer();
    const second = fakeRenderer();
    const a = core.attach(first.r, host());
    await vi.advanceTimersByTimeAsync(0);
    await a;
    const b = core.attach(second.r, host());
    await vi.advanceTimersByTimeAsync(0);
    await b;
    core.detach(first.r);
    expect(core.getRenderer()).toBe(second.r);
  });

  it('упавший рендерер не роняет ядро', async () => {
    const core = new MascotCore({ trackPointer: false });
    const { r, calls } = fakeRenderer();
    r.init = () => Promise.reject(new Error('нет WebGL'));
    const errors: unknown[] = [];
    core.on((e) => { if (e.type === 'error') errors.push(e.error); });
    await core.attach(r, host());
    expect(core.getRenderer()).toBeNull();
    expect(calls.disposed).toBe(1);
    expect(errors).toHaveLength(1);
  });

  it('после реплики возвращается в прежнее состояние', async () => {
    const core = new MascotCore({ trackPointer: false, voice: { provider: fakeVoice(500) } });
    const { r } = fakeRenderer();
    const attached = core.attach(r, host());
    await vi.advanceTimersByTimeAsync(0);
    await attached;
    core.setState('thinking');
    core.say('Секунду.');
    await vi.advanceTimersByTimeAsync(10);
    expect(core.getState()).toBe('speaking');
    await vi.advanceTimersByTimeAsync(600);
    expect(core.getState()).toBe('thinking');
  });

  it('упавший слушатель не мешает остальным', () => {
    const core = new MascotCore({ trackPointer: false });
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    const seen: string[] = [];
    core.on(() => { throw new Error('x'); });
    core.on((e) => seen.push(e.type));
    core.setState('thinking');
    expect(seen).toEqual(['state']);
    err.mockRestore();
  });

  it('неизвестное состояние игнорируется', () => {
    const core = new MascotCore({ trackPointer: false });
    core.setState('nope' as never);
    expect(core.getState()).toBe('idle');
  });
});

describe('reducedMotion', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('без микро-саккад взгляд стоит, пока не двинули курсор', async () => {
    const core = new MascotCore({ trackPointer: false, reducedMotion: true });
    const { r } = fakeRenderer();
    const gazes: number[] = [];
    r.setGaze = (x) => { gazes.push(x); };
    const attached = core.attach(r, host());
    await vi.advanceTimersByTimeAsync(0);
    await attached;
    // При подключении взгляд один раз ставится в камеру — дальше ни одной саккады.
    const afterAttach = [...gazes];
    await vi.advanceTimersByTimeAsync(10_000);
    expect(gazes).toEqual(afterAttach);
    expect(gazes.every((x) => x === 0)).toBe(true);
  });
});

describe('танцы и resetAfterMs: clip', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  async function ready() {
    const core = new MascotCore({ trackPointer: false });
    const fake = fakeRenderer();
    const attached = core.attach(fake.r, host());
    await vi.advanceTimersByTimeAsync(0);
    await attached;
    return { core, ...fake };
  }

  it('dance берёт танец из DANCES и не повторяет предыдущий подряд', async () => {
    const { core, calls } = await ready();
    const picked: string[] = [];
    for (let i = 0; i < 20; i++) {
      core.setState('dance');
      picked.push(calls.clips.at(-1)!);
    }
    expect(picked.every((c) => DANCES.includes(c))).toBe(true);
    for (let i = 1; i < picked.length; i++) expect(picked[i]).not.toBe(picked[i - 1]);
    expect(new Set(picked).size).toBeGreaterThan(1);
  });

  it('состояние держится, пока клип не доиграет, и уходит в idle сразу после', async () => {
    const { core, r, calls } = await ready();
    core.setState('success');
    await vi.advanceTimersByTimeAsync(10_000);   // дольше прежних 3.5 с
    expect(core.getState()).toBe('success');
    r.onClipEnd?.(calls.clips.at(-1)!);
    expect(core.getState()).toBe('idle');
  });

  it('конец чужого клипа состояние не сбрасывает', async () => {
    const { core, r } = await ready();
    core.setState('dance');
    r.onClipEnd?.('wave');
    expect(core.getState()).toBe('dance');
  });

  it('клип не загрузился — запасной таймер всё равно вернёт в idle', async () => {
    const { core } = await ready();
    core.setState('dance');
    await vi.advanceTimersByTimeAsync(61_000);
    expect(core.getState()).toBe('idle');
  });
});

describe('реплика во время танца', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  async function dancing(speechMs: number) {
    const core = new MascotCore({ trackPointer: false, voice: { provider: fakeVoice(speechMs) } });
    const fake = fakeRenderer();
    const attached = core.attach(fake.r, host());
    await vi.advanceTimersByTimeAsync(0);
    await attached;
    core.setState('dance');
    return { core, ...fake };
  }

  it('танец продолжается, новый не начинается', async () => {
    const { core, calls } = await dancing(500);
    const dance = calls.clips.at(-1);
    const clipsBefore = calls.clips.length;
    core.say('Смотри, как я умею!');
    await vi.advanceTimersByTimeAsync(10);
    expect(core.getState()).toBe('dance');
    await vi.advanceTimersByTimeAsync(600);
    expect(core.getState()).toBe('dance');
    expect(calls.clips.length).toBe(clipsBefore);
    expect(calls.clips.at(-1)).toBe(dance);
  });

  it('танец кончился посреди реплики — договаривает и уходит в idle', async () => {
    const { core, r, calls } = await dancing(1000);
    core.say('Длинная реплика.');
    await vi.advanceTimersByTimeAsync(10);
    r.onClipEnd?.(calls.clips.at(-1)!);
    expect(core.getState()).toBe('speaking');
    await vi.advanceTimersByTimeAsync(1100);
    expect(core.getState()).toBe('idle');
  });
});

describe('success с репликой', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('жест не прерывается и не проигрывается заново после реплики', async () => {
    const core = new MascotCore({ trackPointer: false, voice: { provider: fakeVoice(800) } });
    const { r, calls } = fakeRenderer();
    const attached = core.attach(r, host());
    await vi.advanceTimersByTimeAsync(0);
    await attached;

    core.setState('success');
    core.say('Готово!');
    await vi.advanceTimersByTimeAsync(1000);

    expect(core.getState()).toBe('success');
    expect(calls.clips.filter((c) => c === 'peace')).toHaveLength(1);
    expect(calls.clips).not.toContain('talking');
  });
});
