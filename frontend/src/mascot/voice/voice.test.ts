// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';
import { planSpeech } from './ruVisemes';
import { Voice } from './Voice';
import type { TTSHandlers, TTSProvider } from './types';

describe('planSpeech', () => {
  it('русские гласные раскладываются по виземам', () => {
    const plan = planSpeech('аоуие');
    expect(plan.steps.map((s) => s.viseme)).toEqual(['aa', 'oh', 'ou', 'ih', 'ee']);
  });

  it('губные согласные закрывают рот', () => {
    const plan = planSpeech('мбп');
    expect(plan.steps.every((s) => s.viseme === null && s.weight === 0)).toBe(true);
  });

  it('знаки препинания дают паузы и акценты', () => {
    const plan = planSpeech('Да, конечно. Правда?');
    expect(plan.accents.map((a) => a.kind)).toEqual(['pause', 'nod', 'question']);
  });

  it('быстрая речь — короче раскладка', () => {
    const slow = planSpeech('Привет, я Юми.', 1).totalMs;
    const fast = planSpeech('Привет, я Юми.', 2).totalMs;
    expect(fast).toBeCloseTo(slow / 2, 5);
  });

  it('каждый шаг помнит позицию в тексте', () => {
    const plan = planSpeech('ма ма');
    expect(plan.steps.map((s) => s.charIndex)).toEqual([0, 1, 2, 3, 4]);
  });
});

describe('Voice', () => {
  /** Провайдер, у которого конец вытесненной реплики приходит с опозданием — как в Chrome. */
  function lateProvider(): TTSProvider & { handlers: TTSHandlers[] } {
    const handlers: TTSHandlers[] = [];
    return {
      name: 'late',
      hasBoundaries: false,
      handlers,
      speak: (_text, h) => { handlers.push(h); return new Promise(() => {}); },
      cancel: vi.fn(),
    };
  }

  it('конец старой реплики не обрывает новую', () => {
    const provider = lateProvider();
    const voice = new Voice({ provider });
    const speaking: boolean[] = [];
    voice.setSink({ visemes: () => {}, accent: () => {}, speaking: (a) => speaking.push(a) });

    void voice.say('первая');
    void voice.say('вторая');
    provider.handlers[0]!.onEnd?.();          // запоздавший конец первой
    expect(voice.isSpeaking).toBe(true);

    provider.handlers[1]!.onEnd?.();
    expect(voice.isSpeaking).toBe(false);
    expect(speaking.at(-1)).toBe(false);
  });

  it('cancel глушит провайдера и закрывает рот', () => {
    const provider = lateProvider();
    const voice = new Voice({ provider });
    const visemes: object[] = [];
    voice.setSink({ visemes: (w) => visemes.push(w), accent: () => {}, speaking: () => {} });
    void voice.say('текст');
    voice.cancel();
    expect(provider.cancel).toHaveBeenCalled();
    expect(voice.isSpeaking).toBe(false);
    expect(visemes.at(-1)).toEqual({});
  });

  it('смена провайдера посреди речи закрывает рот, даже если старый не прислал onEnd', () => {
    // lateProvider при отмене onEnd не шлёт — как записанные реплики или
    // серверный TTS, которым контракт этого не предписывает.
    const old = lateProvider();
    const voice = new Voice({ provider: old });
    const speaking: boolean[] = [];
    voice.setSink({ visemes: () => {}, accent: () => {}, speaking: (a) => speaking.push(a) });
    void voice.say('длинная реплика');
    voice.setProvider(lateProvider());
    expect(old.cancel).toHaveBeenCalled();
    expect(voice.isSpeaking).toBe(false);
    expect(speaking.at(-1)).toBe(false);
    // Запоздавший конец старой реплики ничего не ломает.
    old.handlers[0]!.onEnd?.();
    expect(speaking.filter((a) => !a)).toHaveLength(1);
  });

  it('пустая реплика ничего не запускает', async () => {
    const provider = lateProvider();
    const voice = new Voice({ provider });
    await voice.say('   ');
    expect(provider.handlers).toHaveLength(0);
  });
});

describe('выбор голоса', () => {
  const v = (name: string, lang = 'ru-RU', isDefault = false) => ({ name, lang, default: isDefault });
  const windows = [
    v('Google русский'),
    v('Microsoft Irina - Russian (Russia)'),
    v('Microsoft Pavel - Russian (Russia)'),
    v('Google US English', 'en-US', true),
  ];

  it('по умолчанию — Ирина, даже если есть Google', async () => {
    const { BrowserTTS } = await import('./BrowserTTS');
    expect(BrowserTTS.choose(windows)?.name).toContain('Irina');
  });

  it('без Ирины (Mac, Linux) — лучший русский по качеству', async () => {
    const { BrowserTTS } = await import('./BrowserTTS');
    expect(BrowserTTS.choose(windows.filter((x) => !x.name.includes('Irina')))?.name).toBe('Google русский');
  });

  it('точное имя важнее предпочтений; своё предпочтение заменяет Ирину', async () => {
    const { BrowserTTS } = await import('./BrowserTTS');
    expect(BrowserTTS.choose(windows, { voiceName: 'Microsoft Pavel - Russian (Russia)' })?.name).toContain('Pavel');
    expect(BrowserTTS.choose(windows, { prefer: ['Pavel'] })?.name).toContain('Pavel');
  });

  it('русского нет — голос по умолчанию', async () => {
    const { BrowserTTS } = await import('./BrowserTTS');
    expect(BrowserTTS.choose([v('Samantha', 'en-US'), v('Alex', 'en-US', true)])?.name).toBe('Alex');
  });
});
