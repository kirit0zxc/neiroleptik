// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
import { parseTransitionsFile, transitionKey } from './transitions';
import { defineTransition, removeTransition } from './transitionTable';
import { MascotCore } from './MascotCore';
import type { MascotRenderer } from './types';

describe('parseTransitionsFile', () => {
  it('годное проходит, битое отбрасывается', () => {
    const file = parseTransitionsFile({
      transitions: {
        [transitionKey('thinking', 'success')]: 600,
        [transitionKey('a', 'b')]: 99_999,
        'без стрелки': 300,
        [transitionKey('a', 'c')]: 'быстро',
      },
      chains: {
        знакомство: [
          { kind: 'state', name: 'welcome', mode: 'end', secs: 3, fadeMs: null },
          { kind: 'clip', name: 'nod', mode: 'secs', secs: 2, fadeMs: 400 },
          { kind: 'танец', name: 'x' },
        ],
        'плохое/имя': [{ kind: 'state', name: 'idle' }],
        пустая: [],
      },
    });
    expect(file.transitions).toEqual({ 'thinking→success': 600, 'a→b': 3000 });
    expect(Object.keys(file.chains)).toEqual(['знакомство']);
    expect(file.chains.знакомство).toHaveLength(2);
  });

  it('мусор вместо файла — пустая таблица', () => {
    expect(parseTransitionsFile(null)).toEqual({ transitions: {}, chains: {} });
    expect(parseTransitionsFile([1])).toEqual({ transitions: {}, chains: {} });
  });
});

describe('таблица переходов в ядре', () => {
  it('пара из таблицы важнее плавности состояния, разовая — важнее таблицы', async () => {
    const playClip = vi.fn();
    const r = {
      init: async () => {}, setExpression() {}, setGaze() {}, setMouth() {},
      setVisemes() {}, setSpeaking() {}, accent() {}, blink() {}, playClip,
      stopClip() {}, setGlow() {},
      capabilities: () => ({ clips: [], expressions: [] }), dispose() {},
    } as MascotRenderer;
    const core = new MascotCore({ trackPointer: false });
    await core.attach(r, document.createElement('div'));

    defineTransition('thinking', 'success', 650);
    core.setState('thinking');
    core.setState('success');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ fadeMs: 650 });

    // Из другого состояния пары нет — обычная плавность.
    core.setState('searching');
    core.setState('success');
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ fadeMs: 280 });

    core.setState('thinking');
    core.setState('success', { fadeMs: 90 });
    expect(playClip.mock.lastCall?.[1]).toMatchObject({ fadeMs: 90 });

    removeTransition('thinking', 'success');
    core.dispose();
  });
});
