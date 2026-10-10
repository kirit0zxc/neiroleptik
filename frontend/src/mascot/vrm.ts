/**
 * 3D-отрисовка Юми на three.js + VRM.
 *
 *   import { mount } from 'mascot/vrm';
 *   await mount(document.getElementById('yumi'), { assetsBase: '/static/mascot/' });
 */
import { VrmRenderer, type VrmRendererOptions } from './renderers/vrm/VrmRenderer';
import { getMascot } from './singleton';

export { VrmRenderer };
export type { VrmRendererOptions };
export { CAMERA_DEFAULTS, CAMERA_LIMITS, parseCamera, cameraDiff } from './renderers/vrm/camera';
export type { CameraSettings } from './renderers/vrm/camera';
export { parseClipCameras } from './renderers/vrm/clipCameras';
export type { ClipCameras } from './renderers/vrm/clipCameras';

/** Вставить Юми в элемент и запустить. */
export async function mount(host: HTMLElement, options?: VrmRendererOptions) {
  const core = getMascot();
  await core.attach(new VrmRenderer(options), host);
  return core;
}

/** Убрать Юми со страницы; ядро и подписки моста остаются живы. */
export function unmount() {
  getMascot().detach();
}
