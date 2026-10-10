// @vitest-environment happy-dom
/**
 * Регрессионные тесты по ревью трёх проверяющих (AGENTS.md, раздел 4).
 * Каждый тест воспроизводит подтверждённую дыру; номер — из сводки.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MascotCore } from './MascotCore';
import { ERROR_RESET_MS, PRESETS, STATES, defineState, removeState } from './states';
import { toStateSpec } from './presets';
import { MascotBridge } from '../bridge/MascotBridge';
import { DEFAULT_RULES } from '../bridge/rules';
import { clipOf } from '../renderers/vrm/VrmRenderer';
import type { MascotRenderer } from './types';
import type { TTSHandlers, TTSProvider } from '../voice/types';

/** Рендерер-заглушка, записывающий всё, что с ним делают. */
function fakeRenderer() {
  const log = {
    clips: [] as { name: string; opts: Record<string, unknown> }[],
    gaze: [] as [number, number][],
    expressions: [] as string[],
  };
  const r: MascotRenderer = {
    init: async () => {},
    setExpression: (name) => { log.expressions.push(name); },
    setGaze: (x, y) => { log.gaze.push([x, y]); },
    setMouth() {}, setVisemes() {}, setSpeaking() {}, accent() {}, blink() {},
    playClip: (name, opts = {}) => { log.clips.push({ name, opts }); },
    stopClip() {}, setGlow() {},
    capabilities: () => ({ clips: [], expressions: [] }),
    dispose() {},
  };
  return { r, log };
}

async function attached(core: MascotCore) {
  const fake = fakeRenderer();
  const job = core.attach(fake.r, document.createElement('div'));
  await vi.advanceTimersByTimeAsync(0);
  await job;
  return fake;
}

/** Голос, который говорит ms миллисекунд — или падает. */
function voice(ms: number, fail = false): TTSProvider {
  return {
    name: 'fake', hasBoundaries: false,
    speak: (_t: string, h: TTSHandlers) => new Promise((resolve, reject) => {
      h.onStart?.();
      setTimeout(() => (fail ? reject(new Error('TTS недоступен')) : (h.onEnd?.(), resolve())), ms);
    }),
    cancel() {},
  };
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('мост и fetch', () => {
  const bridge = () => {
    const states: string[] = []; const said: string[] = [];
    const core = { setState: (s: string) => states.push(s), say: (t: string) => said.push(t), playGesture() {} } as unknown as MascotCore;
    return { b: new MascotBridge(core, DEFAULT_RULES), states, said };
  };
  const ok = () => Promise.resolve(new Response(null, { status: 200 }));

  it('1: blob: и data: (текстуры самой Юми) не двигают её', async () => {
    const { b, states } = bridge();
    const f = b.wrapFetch(ok as typeof fetch);
    await f('blob:http://localhost/abc');
    await f('data:image/png;base64,AAAA');
    expect(states).toEqual([]);
  });

  it('2: непрозрачный ответ (no-cors) — не ошибка', async () => {
    const { b, states } = bridge();
    const opaque = { ok: false, status: 0, type: 'opaque' } as Response;
    const f = b.wrapFetch((() => Promise.resolve(opaque)) as typeof fetch);
    await f('https://stats.example/pixel');
    expect(states).toEqual(['thinking', 'success']);
  });

  it('2: текст ошибки не читается вслух', async () => {
    const { b, said } = bridge();
    const f = b.wrapFetch((() => Promise.reject(new TypeError('Failed to fetch'))) as typeof fetch);
    await expect(f('/api/x')).rejects.toThrow();
    expect(said).toHaveLength(1);
    expect(said[0]).not.toMatch(/fetch|TypeError/i);
  });

  it('2: обычное закрытие сокета — покой, обрыв — ошибка', () => {
    const { b, states } = bridge();
    b.handle('ws:close', { code: 1000, wasClean: true });
    b.handle('ws:close', { code: 1006, wasClean: false });
    expect(states).toEqual(['idle', 'error']);
  });

  it('повторный wrapFetch тем же мостом не наращивает обёртки', () => {
    const { b } = bridge();
    const once = b.wrapFetch(ok as typeof fetch);
    expect(b.wrapFetch(once)).toBe(once);
  });

  it('throttle: правило, вернувшее null, не вытесняет отложенный done', () => {
    const states: string[] = [];
    const core = { setState: (s: string) => states.push(s), say() {}, playGesture() {} } as unknown as MascotCore;
    const b = new MascotBridge(core, { start: 'thinking', done: 'success', noise: () => null }, { throttleMs: 200 });
    b.handle('start'); b.handle('done'); b.handle('noise');
    vi.advanceTimersByTime(250);
    expect(states).toEqual(['thinking', 'success']);
  });

  it('имена из прототипа не считаются правилами: срабатывает fallback', () => {
    const states: string[] = [];
    const core = { setState: (s: string) => states.push(s), say() {}, playGesture() {} } as unknown as MascotCore;
    const b = new MascotBridge(core, {}, { fallback: 'curious' });
    b.handle('constructor'); b.handle('toString');
    expect(states).toEqual(['curious', 'curious']);
  });
});

describe('ядро', () => {
  it('прототипные имена состояний игнорируются', async () => {
    const core = new MascotCore({ trackPointer: false });
    await attached(core);
    core.setState('constructor');
    core.setState('toString');
    expect(core.getState()).toBe('idle');
  });

  it('2: после ошибки Юми возвращается в покой', async () => {
    const core = new MascotCore({ trackPointer: false });
    await attached(core);
    core.setState('error');
    await vi.advanceTimersByTimeAsync(ERROR_RESET_MS + 10);
    expect(core.getState()).toBe('idle');
  });

  it('3: за курсором — только в покое; в жестах и речи — в камеру', async () => {
    const core = new MascotCore({ trackPointer: true, reducedMotion: true });
    const { log } = await attached(core);
    (core as unknown as { pointer: { x: number; y: number } }).pointer = { x: 0.8, y: 0.5 };
    core.setState('idle');
    expect(log.gaze.at(-1)).toEqual([0.8, 0.5]);
    core.setState('thinking');
    expect(log.gaze.at(-1)).toEqual([0, 0]);
    core.setState('speaking');
    expect(log.gaze.at(-1)).toEqual([0, 0]);
  });

  it('3: своё состояние с followPointer следит за курсором', async () => {
    const core = new MascotCore({ trackPointer: true, reducedMotion: true });
    const { log } = await attached(core);
    defineState('look', toStateSpec({ expression: 'happy', weight: 0.5, glow: {}, followPointer: true })!);
    (core as unknown as { pointer: { x: number; y: number } }).pointer = { x: -0.4, y: 0 };
    core.setState('look');
    expect(log.gaze.at(-1)).toEqual([-0.4, 0]);
    removeState('look');
  });

  it('5: упавший провайдер голоса не оставляет Юми в speaking', async () => {
    const core = new MascotCore({ trackPointer: false, voice: { provider: voice(100, true) } });
    await attached(core);
    const errors: unknown[] = [];
    core.on((e) => { if (e.type === 'error') errors.push(e.error); });
    core.say('Привет');
    await vi.advanceTimersByTimeAsync(150);
    expect(core.isSpeaking).toBe(false);
    expect(core.getState()).toBe('idle');
    expect(errors).toHaveLength(1);
  });

  it('7: после реплики разовый жест не повторяется, петля продолжается', async () => {
    const core = new MascotCore({ trackPointer: false, voice: { provider: voice(300) } });
    const { log } = await attached(core);

    core.setState('welcome');                 // машет
    core.say('Привет!');
    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(400);
    expect(core.getState()).toBe('idle');
    expect(log.clips.filter((c) => c.name === 'wave')).toHaveLength(1);

    core.setState('thinking');
    core.say('Секунду.');
    await vi.advanceTimersByTimeAsync(400);
    expect(core.getState()).toBe('thinking');
  });

  it('7: во время речи лицо остаётся от прежнего состояния', async () => {
    const core = new MascotCore({ trackPointer: false, voice: { provider: voice(300) } });
    const { log } = await attached(core);
    core.setState('error');
    core.say('Не получилось.');
    await vi.advanceTimersByTimeAsync(10);
    expect(core.getState()).toBe('speaking');
    expect(log.expressions.at(-1)).toBe('sad');
  });

  it('6: без картинки временное состояние всё равно уходит в покой', async () => {
    const core = new MascotCore({ trackPointer: false });
    core.setState('welcome');                 // рендерера нет
    await vi.advanceTimersByTimeAsync(3300);
    expect(core.getState()).toBe('idle');

    core.setState('success');                 // «по концу клипа», а клипа не будет
    await vi.advanceTimersByTimeAsync(3100);
    expect(core.getState()).toBe('idle');
  });

  it('6: при отсоединении приходит detached, а «клиповое» состояние не висит минуту', async () => {
    const core = new MascotCore({ trackPointer: false });
    await attached(core);
    const events: string[] = [];
    core.on((e) => events.push(e.type));
    core.setState('success');
    core.detach();
    expect(events).toContain('detached');
    await vi.advanceTimersByTimeAsync(3100);
    expect(core.getState()).toBe('idle');
  });

  it('жест из правила { state, clip } завершает «клиповое» состояние', async () => {
    const core = new MascotCore({ trackPointer: false });
    const { r, log } = await attached(core);
    core.setState('success');
    core.playGesture('nod');
    expect(log.clips.at(-1)).toMatchObject({ name: 'nod', opts: { loop: false, returnTo: 'idle' } });
    r.onClipEnd?.('nod');
    expect(core.getState()).toBe('idle');
  });

  it('разовый жест состояния возвращается в покой, а не в петлю прошлого', async () => {
    const core = new MascotCore({ trackPointer: false });
    const { log } = await attached(core);
    core.setState('thinking');
    core.setState('welcome');
    expect(log.clips.at(-1)).toMatchObject({ name: 'wave', opts: { returnTo: 'idle' } });
  });

  it('состояние без анимации возвращает тело в покой', async () => {
    const core = new MascotCore({ trackPointer: false });
    const { log } = await attached(core);
    core.setState('dance');
    defineState('face_only', toStateSpec({ expression: 'happy', weight: 0.5, glow: {} })!);
    core.setState('face_only');
    expect(log.clips.at(-1)).toMatchObject({ name: 'idle', opts: { loop: true } });
    removeState('face_only');
  });
});

describe('мелочи', () => {
  it('путь к клипу с # и @ не режется при разборе ключа', () => {
    expect(clipOf('anim/vrma/a#b@c.vrma')).toBe('anim/vrma/a#b@c.vrma');
    expect(clipOf('anim/vrma/a#b.vrma#none@1-2.5')).toBe('anim/vrma/a#b.vrma');
    expect(clipOf('peace@2.5-6')).toBe('peace');
  });

  it('removeState убирает и пресет из файла', () => {
    PRESETS.tmp_preset = STATES.idle;
    defineState('tmp_preset', STATES.idle);
    removeState('tmp_preset');
    expect('tmp_preset' in PRESETS).toBe(false);
    expect('tmp_preset' in STATES).toBe(false);
  });
});
