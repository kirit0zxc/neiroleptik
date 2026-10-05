// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { isPresetName, parsePresets, toStateSpec } from './presets';
import { BUILTIN_STATES, STATES, defineState, removeState } from './states';

const valid = {
  expression: 'happy', weight: 0.8, action: 'victory', loop: false,
  resetAfterMs: 'clip', speakOver: true,
  glow: { color: '#D6457F', intensity: 0.6, pulseHz: 1 },
};

describe('пресеты', () => {
  it('корректная запись проходит как есть', () => {
    expect(toStateSpec(valid)).toEqual(valid);
  });

  it('значения вне диапазона зажимаются, мусор заменяется запасным', () => {
    const s = toStateSpec({ expression: 'sad', weight: 7, resetAfterMs: 'потом', glow: { color: 'red', pulseHz: -1 } });
    expect(s).toMatchObject({ weight: 1, resetAfterMs: null, glow: { color: '#9ED8BC', pulseHz: 0 } });
  });

  it('неизвестное выражение — запись отбрасывается', () => {
    expect(toStateSpec({ ...valid, expression: 'smug' })).toBeNull();
  });

  it('имена: буквы любого алфавита, цифры, _ и -', () => {
    expect(isPresetName('удивлённый_кивок')).toBe(true);
    expect(isPresetName('celebrate-2')).toBe(true);
    expect(isPresetName('__studio')).toBe(false);
    expect(isPresetName('с пробелом')).toBe(false);
    expect(isPresetName('')).toBe(false);
  });

  it('файл с битыми записями: выживают только годные', () => {
    const p = parsePresets({ good: valid, 'bad name': valid, broken: 42, __hidden: valid });
    expect(Object.keys(p)).toEqual(['good']);
    expect(parsePresets([1, 2])).toEqual({});
    expect(parsePresets(null)).toEqual({});
  });

  it('defineState добавляет, removeState возвращает встроенное', () => {
    defineState('my_state', toStateSpec(valid)!);
    expect(STATES.my_state).toBeDefined();
    removeState('my_state');
    expect(STATES.my_state).toBeUndefined();

    defineState('thinking', toStateSpec(valid)!);
    expect(STATES.thinking.action).toBe('victory');
    removeState('thinking');
    expect(STATES.thinking).toBe(BUILTIN_STATES.thinking);
  });
});

describe('пресеты из сети', () => {
  it('ключ __proto__ через JSON.parse не становится пресетом', () => {
    const raw = JSON.parse('{"__proto__": {"expression": "happy", "weight": 1, "glow": {"color": "#fff", "intensity": 1, "pulseHz": 0}}}');
    const parsed = parsePresets(raw);
    expect(Object.keys(parsed)).toEqual([]);
    expect((parsed as Record<string, unknown>).expression).toBeUndefined();
  });
});

describe('реестр клипов', () => {
  it('имена из прототипа не находятся как клипы', async () => {
    const { resolveClip } = await import('./clips');
    expect(resolveClip('constructor')).toBeNull();
    expect(resolveClip('toString')).toBeNull();
    expect(resolveClip('peace')).not.toBeNull();
  });
});
