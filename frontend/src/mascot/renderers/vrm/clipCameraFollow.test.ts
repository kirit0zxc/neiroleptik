// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VrmRenderer } from './VrmRenderer';
import { loadClip } from './loadClip';

// Загрузка клипа без сети: по умолчанию «в файле нет анимации».
vi.mock('./loadClip', () => ({ loadClip: vi.fn(async () => null), forgetClip: vi.fn() }));

/**
 * Поведение рендерера без three.js: frameCamera подменяется шпионом,
 * который лишь запоминает, каким кадром камера расставлена.
 */
type Inner = {
  currentName: string;
  bounds: unknown;
  camKey: string;
  actions: Map<string, { stop(): void; getClip(): object }>;
  vrm: unknown;
  mixer: unknown;
  frameCamera(snap?: boolean): void;
  followClipCamera(): void;
  evictVariants(keep: string): void;
  switchTo(name: string, opts: { loop?: boolean; fadeMs?: number }): void;
};

/** Действие микшера-заглушка: любой метод возвращает само действие. */
function fakeAction(): never {
  const target = { getClip: () => ({ duration: 1 }), isRunning: () => true, loop: 0 };
  const proxy: unknown = new Proxy(target, {
    get: (obj, key) => (key in obj ? (obj as Record<string | symbol, unknown>)[key] : () => proxy),
    set: () => true,
  });
  return proxy as never;
}

/** Рендерер «играет» клип key: действие в таблице и выбранный кадр. */
function playing(inner: Inner, key: string) {
  inner.actions.set(key, fakeAction());
  inner.currentName = key;
  inner.followClipCamera();
}

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

  it('смена клипа (switchTo) переезжает к его кадру', () => {
    const { r, inner, frame } = setup(false);
    r.setClipCamera('test-follow', { zoom: 2 });
    inner.followClipCamera();
    frame.mockClear();
    inner.actions.set('test-follow', fakeAction());
    inner.switchTo('test-follow', { loop: true });
    expect(frame).toHaveBeenCalledWith(false);
    expect(inner.camKey).toBe(JSON.stringify(r.getEffectiveCamera()));
  });

  it('forgetFile играющего файла возвращает общий кадр', () => {
    const { r, inner, frame } = setup(false);
    r.setClipCamera('anim/vrma/test-forget.vrma', { zoom: 2 });
    try {
      playing(inner, 'file:/mascot/anim/vrma/test-forget.vrma:inplace');
      frame.mockClear();
      r.forgetFile('/mascot/anim/vrma/test-forget.vrma');
      expect(frame).toHaveBeenCalledTimes(1);
      expect(inner.camKey).toBe(JSON.stringify(r.getCamera()));
    } finally {
      r.setClipCamera('anim/vrma/test-forget.vrma', null);
    }
  });

  it('playFile с reload камеру сразу не трогает: клип заиграет снова', () => {
    const { r, inner, frame } = setup(false);
    r.setClipCamera('anim/vrma/test-forget.vrma', { zoom: 2 });
    try {
      playing(inner, 'file:/mascot/anim/vrma/test-forget.vrma:inplace');
      frame.mockClear();
      r.playFile('/mascot/anim/vrma/test-forget.vrma', { reload: true });
      expect(frame).not.toHaveBeenCalled();
      // Файл действительно сброшен — иначе сыграл бы устаревший клип.
      expect(inner.actions.has('file:/mascot/anim/vrma/test-forget.vrma:inplace')).toBe(false);
      expect(inner.currentName).toBe('');
    } finally {
      r.setClipCamera('anim/vrma/test-forget.vrma', null);
    }
  });
});

describe('перезагрузка файла не удалась', () => {
  const key = 'file:/mascot/anim/vrma/test-broken.vrma:inplace';
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

  for (const [title, impl] of [
    ['в файле нет анимации', async () => null],
    ['файл не загрузился', async () => { throw new Error('404'); }],
  ] as const) {
    it(`${title} — камера возвращается к общему кадру`, async () => {
      vi.mocked(loadClip).mockImplementationOnce(impl as never);
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      const { r, inner, frame } = setup(false);
      inner.vrm = {};
      inner.mixer = { clipAction: () => fakeAction(), uncacheAction: () => {}, uncacheClip: () => {} };
      r.setClipCamera('anim/vrma/test-broken.vrma', { zoom: 2 });
      try {
        playing(inner, key);
        frame.mockClear();
        r.playFile('/mascot/anim/vrma/test-broken.vrma', { reload: true });
        await flush();
        expect(frame).toHaveBeenCalledTimes(1);
        expect(inner.camKey).toBe(JSON.stringify(r.getCamera()));
      } finally {
        r.setClipCamera('anim/vrma/test-broken.vrma', null);
      }
    });
  }
});

describe('перезагрузка файла удалась', () => {
  it('камера не качается: один переезд — к кадру клипа, после switchTo', async () => {
    vi.mocked(loadClip).mockImplementationOnce(async () => ({ duration: 1 }) as never);
    const { r, inner, frame } = setup(false);
    inner.vrm = {};
    inner.mixer = { clipAction: () => fakeAction(), uncacheAction: () => {}, uncacheClip: () => {} };
    r.setClipCamera('anim/vrma/test-ok.vrma', { zoom: 2 });
    try {
      playing(inner, 'file:/mascot/anim/vrma/test-ok.vrma:inplace');
      frame.mockClear();
      r.playFile('/mascot/anim/vrma/test-ok.vrma', { reload: true });
      await new Promise((resolve) => setTimeout(resolve, 0));
      // Кадр клипа не изменился — камера вообще не должна была двигаться;
      // без защиты в ensureClip она съездила бы к общему кадру и обратно.
      expect(frame).not.toHaveBeenCalled();
      expect(inner.currentName).toBe('file:/mascot/anim/vrma/test-ok.vrma:inplace');
    } finally {
      r.setClipCamera('anim/vrma/test-ok.vrma', null);
    }
  });

  it('после dispose камера не трогается', () => {
    const { r, inner, frame } = setup(false);
    r.setClipCamera('test-follow', { zoom: 2 });
    inner.currentName = 'test-follow';
    r.dispose();
    inner.followClipCamera();
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

describe('перезагрузка файла во время его загрузки', () => {
  it('играет новая версия, а не догрузившаяся старая', async () => {
    // Две загрузки, которые завершаем вручную в нужном порядке.
    const loads: Array<(clip: unknown) => void> = [];
    vi.mocked(loadClip).mockImplementation(() => new Promise((resolve) => loads.push(resolve as never)));
    const made: unknown[] = [];
    const r = new VrmRenderer();
    const inner = r as unknown as Inner;
    inner.vrm = {};
    inner.mixer = {
      clipAction: (clip: unknown) => { made.push(clip); return fakeAction(); },
      uncacheAction: () => {}, uncacheClip: () => {},
    };
    try {
      r.playFile('/mascot/anim/vrma/test-slow.vrma');                   // старая версия грузится
      r.playFile('/mascot/anim/vrma/test-slow.vrma', { reload: true });  // файл подменили
      expect(loads).toHaveLength(2);
      const oldClip = { duration: 1, v: 'old' };
      const newClip = { duration: 1, v: 'new' };
      loads[0]!(oldClip);
      loads[1]!(newClip);
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(made).toEqual([newClip]);
    } finally {
      vi.mocked(loadClip).mockImplementation(async () => null);
    }
  });

  it('старая загрузка завершилась позже новой — клип не перезапускается со старыми настройками', async () => {
    const loads: Array<(clip: unknown) => void> = [];
    vi.mocked(loadClip).mockImplementation(() => new Promise((resolve) => loads.push(resolve as never)));
    const plays: Array<{ loop: unknown }> = [];
    const r = new VrmRenderer();
    const inner = r as unknown as Inner;
    inner.vrm = {};
    inner.mixer = {
      clipAction: () => fakeAction(),
      uncacheAction: () => {}, uncacheClip: () => {},
    };
    const switchTo = vi.spyOn(inner, 'switchTo');
    try {
      r.playFile('/mascot/anim/vrma/test-race.vrma', { loop: false });
      r.playFile('/mascot/anim/vrma/test-race.vrma', { loop: true, reload: true });
      loads[1]!({ duration: 1 });   // новая версия раньше
      await new Promise((resolve) => setTimeout(resolve, 0));
      loads[0]!({ duration: 1 });   // старая позже
      await new Promise((resolve) => setTimeout(resolve, 0));
      for (const call of switchTo.mock.calls) plays.push({ loop: call[1].loop });
      expect(plays).toEqual([{ loop: true }]);
    } finally {
      vi.mocked(loadClip).mockImplementation(async () => null);
    }
  });
});
