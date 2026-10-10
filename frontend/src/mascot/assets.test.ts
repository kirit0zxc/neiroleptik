// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { baseOf, resolveAsset } from './assets';
import { keepTrack } from './renderers/vrm/mask';

describe('resolveAsset', () => {
  it('относительный путь — от assetsBase', () => {
    expect(resolveAsset('model/yumi.vrm', '/mascot/')).toBe('/mascot/model/yumi.vrm');
    expect(resolveAsset('model/yumi.vrm', '/static/mascot')).toBe('/static/mascot/model/yumi.vrm');
    expect(resolveAsset('a.vrma', 'chrome-extension://abc/mascot/')).toBe('chrome-extension://abc/mascot/a.vrma');
  });

  it('абсолютный путь и URL со схемой — как есть', () => {
    for (const url of ['/x.vrm', 'https://cdn/x.vrm', 'blob:abc', 'data:application/octet-stream;base64,AA']) {
      expect(resolveAsset(url, '/mascot/')).toBe(url);
    }
  });

  it('baseOf отдаёт папку файла', () => {
    expect(baseOf('/mascot/model/yumi.vrm')).toBe('/mascot/model/');
  });
});

describe('keepTrack', () => {
  it('inplace режет только смещение таза', () => {
    expect(keepTrack('hips', 'position', 'inplace')).toBe(false);
    expect(keepTrack('hips', 'quaternion', 'inplace')).toBe(true);
    expect(keepTrack('leftFoot', 'quaternion', 'inplace')).toBe(true);
  });

  it('legs убирает ноги, но оставляет поворот таза', () => {
    expect(keepTrack('leftUpperLeg', 'quaternion', 'legs')).toBe(false);
    expect(keepTrack('hips', 'quaternion', 'legs')).toBe(true);
  });

  it('strict оставляет только верх тела', () => {
    expect(keepTrack('hips', 'quaternion', 'strict')).toBe(false);
    expect(keepTrack('head', 'quaternion', 'strict')).toBe(true);
  });

  it('none пропускает всё', () => {
    expect(keepTrack('hips', 'position', 'none')).toBe(true);
  });
});

describe('resolveClip', () => {
  it('имя из реестра', async () => {
    const { resolveClip, CLIPS } = await import('./core/clips');
    expect(resolveClip('reacting')).toBe(CLIPS.reacting);
  });

  it('путь к .vrma вне реестра — играет как есть, без смещения таза', async () => {
    const { resolveClip } = await import('./core/clips');
    expect(resolveClip('anim/vrma/New%20Clip.vrma')).toEqual({ url: 'anim/vrma/New%20Clip.vrma', mask: 'inplace' });
  });

  it('неизвестное имя — null', async () => {
    const { resolveClip } = await import('./core/clips');
    expect(resolveClip('nope')).toBeNull();
  });
});
