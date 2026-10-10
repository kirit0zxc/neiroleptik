// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import { CAMERA_DEFAULTS, cameraDiff, parseCamera } from './camera';

describe('parseCamera', () => {
  it('годные значения проходят, лишнее отбрасывается', () => {
    expect(parseCamera({ fit: 'half', zoom: 1.5, yaw: 30, junk: 1 }))
      .toEqual({ fit: 'half', zoom: 1.5, yaw: 30 });
  });

  it('значения зажимаются в пределы', () => {
    expect(parseCamera({ zoom: 99, pitch: -200, fov: 1 }))
      .toEqual({ zoom: 4, pitch: -60, fov: 10 });
  });

  it('битое и чужое — пусто', () => {
    expect(parseCamera({ fit: 'close-up', zoom: 'big', yaw: NaN })).toEqual({});
    expect(parseCamera(null)).toEqual({});
    expect(parseCamera([1, 2])).toEqual({});
  });
});

describe('cameraDiff', () => {
  it('оставляет только отличия от встроенного кадра', () => {
    expect(cameraDiff({ ...CAMERA_DEFAULTS, fit: 'portrait', zoom: 2 }))
      .toEqual({ fit: 'portrait', zoom: 2 });
    expect(cameraDiff({ ...CAMERA_DEFAULTS })).toEqual({});
  });
});

describe('parseClipCameras', () => {
  it('годные кадры клипов проходят, пустые и битые отбрасываются', async () => {
    const { parseClipCameras } = await import('./clipCameras');
    const parsed = parseClipCameras({
      'anim/vrma/67.vrma': { fit: 'full', zoom: 0.9, junk: true },
      peace: { zoom: 99 },
      empty: {},
      broken: 'кадр',
      ...JSON.parse('{"__proto__": {"zoom": 2}}'),
    });
    expect(parsed['anim/vrma/67.vrma']).toEqual({ fit: 'full', zoom: 0.9 });
    expect(parsed.peace).toEqual({ zoom: 4 });
    expect(Object.keys(parsed)).toEqual(['anim/vrma/67.vrma', 'peace']);
    expect(parseClipCameras(null)).toEqual({});
  });
});
