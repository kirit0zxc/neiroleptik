// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { clipCameraKey, parseClipCameras } from './clipCameras';
import clipCamerasFile from './clipCameras.json';
import { VrmRenderer } from './VrmRenderer';

describe('parseClipCameras: защита и настоящий файл', () => {
  it('ключ __proto__ из сети отбрасывается и не меняет прототип', () => {
    // Литерал { __proto__: … } задаёт прототип, а не ключ, — поэтому
    // вход именно через JSON.parse, как приходит из файла или запроса.
    const parsed = parseClipCameras(JSON.parse('{"__proto__": {"zoom": 2}, "peace": {"zoom": 1.5}}'));
    expect(Object.keys(parsed)).toEqual(['peace']);
    expect(Object.getPrototypeOf(parsed)).toBeNull();
    expect((parsed as Record<string, unknown>).zoom).toBeUndefined();
  });

  it('файл clipCameras.json из сборки разбирается без потерь', () => {
    const parsed = parseClipCameras(clipCamerasFile);
    expect(JSON.parse(JSON.stringify(parsed))).toEqual(clipCamerasFile);
  });
});

describe('clipCameraKey', () => {
  it('имя из реестра и путь из состояния — как есть', () => {
    expect(clipCameraKey('peace', '/mascot/')).toBe('peace');
    expect(clipCameraKey('anim/vrma/67.vrma', '/mascot/')).toBe('anim/vrma/67.vrma');
  });

  it('файл из списка песочницы — путь от assetsBase, раскодированный', () => {
    expect(clipCameraKey('file:/mascot/anim/vrma/Happy%20Idle.vrma:inplace', '/mascot/'))
      .toBe('anim/vrma/Happy Idle.vrma');
    // assetsBase без слэша на конце — тот же результат.
    expect(clipCameraKey('file:/static/y/anim/a%23b.vrma:none', '/static/y'))
      .toBe('anim/a#b.vrma');
  });

  it('файл вне assetsBase и битая кодировка — без падения', () => {
    expect(clipCameraKey('file:https://cdn.test/x.vrma:legs', '/mascot/')).toBe('https://cdn.test/x.vrma');
    expect(clipCameraKey('file:/mascot/bad%E0.vrma:strict', '/mascot/')).toBe('bad%E0.vrma');
  });
});

describe('VrmRenderer: кадр клипа поверх общего', () => {
  // Без init: проверяется только слияние настроек, three.js не нужен.
  type Inner = { currentName: string };
  const make = () => new VrmRenderer({ fit: 'portrait', zoom: 1.2 });

  it('кадр клипа заменяет только свои поля, остальные — из общего', () => {
    const r = make();
    r.setClipCamera('test-wave', { zoom: 0.8, yaw: 20 });
    try {
      const common = r.getCamera();
      (r as unknown as Inner).currentName = 'test-wave#inplace@0-1';
      expect(r.getEffectiveCamera()).toEqual({ ...common, zoom: 0.8, yaw: 20 });
      // Клип без своего кадра — общий кадр (в нём и camera.json, и опции).
      (r as unknown as Inner).currentName = 'test-other';
      expect(r.getEffectiveCamera()).toEqual(common);
      expect(common).toMatchObject({ fit: 'portrait', zoom: 1.2 });
    } finally {
      r.setClipCamera('test-wave', null);
    }
  });

  it('файл из списка песочницы находит кадр, сохранённый по пути', () => {
    const r = make();
    r.setClipCamera('anim/vrma/Test Clip.vrma', { pitch: -10 });
    try {
      (r as unknown as Inner).currentName = 'file:/mascot/anim/vrma/Test%20Clip.vrma:inplace';
      expect(r.getEffectiveCamera().pitch).toBe(-10);
    } finally {
      r.setClipCamera('anim/vrma/Test Clip.vrma', null);
    }
  });

  it('пустой или битый кадр клипа удаляет запись', () => {
    const r = make();
    r.setClipCamera('test-empty', { zoom: 2 });
    expect(r.getClipCamera('test-empty')).toEqual({ zoom: 2 });
    r.setClipCamera('test-empty', { zoom: Number.NaN } as never);
    expect(r.getClipCamera('test-empty')).toBeNull();
    expect(Object.keys(r.getClipCameras())).not.toContain('test-empty');
  });
});
