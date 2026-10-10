// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { WindowPlacement } from './placement';

/** Хранилище в памяти: тесты не трогают настоящий localStorage. */
function memoryStorage(initial: Record<string, string> = {}): Storage {
  const data = new Map(Object.entries(initial));
  return {
    get length() { return data.size; },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => { data.delete(k); },
    setItem: (k, v) => { data.set(k, String(v)); },
  };
}

const desktop = { width: 1440, height: 900 };
const phone = { width: 375, height: 812 };
const win = { width: 300, height: 420 };

describe('WindowPlacement', () => {
  it('по умолчанию — слева внизу; на телефоне свёрнуто', () => {
    const p = new WindowPlacement('k', memoryStorage());
    expect(p.load(desktop, win)).toMatchObject({ x: 16, mode: 'open', muted: false });
    expect(p.load(desktop, win).y).toBe(900 - 420 - 24);
    expect(p.load(phone, win).mode).toBe('minimized');
  });

  it('запоминает положение, режим и звук', () => {
    const storage = memoryStorage();
    new WindowPlacement('k', storage).save({ x: 200, y: 100, mode: 'closed', muted: true });
    expect(new WindowPlacement('k', storage).load(desktop, win))
      .toEqual({ x: 200, y: 100, mode: 'closed', muted: true });
  });

  it('желаемое положение не перезатирается узким экраном, зажимает отрисовка', () => {
    const storage = memoryStorage({ k: JSON.stringify({ x: 1300, y: 800, mode: 'open' }) });
    const store = new WindowPlacement('k', storage);
    const p = store.load({ width: 800, height: 600 }, win);
    expect(p).toMatchObject({ x: 1300, y: 800 });
    expect(store.clamp(p, { width: 800, height: 600 }, win)).toEqual({ x: 800 - 300 - 8, y: 600 - 420 - 8 });
  });

  it('запоминает выбранный кадр, мусор отбрасывает', () => {
    const storage = memoryStorage({ k: JSON.stringify({ x: 1, y: 1, mode: 'open', fit: 'half' }) });
    expect(new WindowPlacement('k', storage).load(desktop, win).fit).toBe('half');
    const bad = memoryStorage({ k: JSON.stringify({ x: 1, y: 1, mode: 'open', fit: 'крупно' }) });
    expect(new WindowPlacement('k', bad).load(desktop, win)).not.toHaveProperty('fit');
  });

  it('битые данные и недоступное хранилище — положение по умолчанию', () => {
    expect(new WindowPlacement('k', memoryStorage({ k: '{сломано' })).load(desktop, win).mode).toBe('open');
    expect(new WindowPlacement('k', memoryStorage({ k: '{"mode":"взорвано","x":"a"}' })).load(desktop, win))
      .toMatchObject({ mode: 'open', x: 16 });
    const p = new WindowPlacement('k', null);
    expect(() => p.save({ x: 1, y: 1, mode: 'open', muted: false })).not.toThrow();
    expect(p.load(desktop, win).mode).toBe('open');
  });

  it('окно больше экрана — прижимается к левому верхнему углу, а не уезжает', () => {
    const p = new WindowPlacement('k', memoryStorage());
    expect(p.clamp({ x: 50, y: 50 }, { width: 200, height: 200 }, win)).toEqual({ x: 8, y: 8 });
  });
});
