import * as THREE from 'three';
import { VRMHumanBoneList, type VRM } from '@pixiv/three-vrm';
import {
  VRMAnimationLoaderPlugin, createVRMAnimationClip, type VRMAnimation,
} from '@pixiv/three-vrm-animation';
import { keepTrack } from './mask';
import { createGLTFLoader } from './gltf';
import type { ClipMask, ClipSpec } from '../../core/clips';
import { baseOf, fetchBuffer } from '../../assets';

/**
 * Разобранные .vrma, общие для всех рендереров.
 *
 * Анимация не зависит от конкретной модели — к скелету её привязывает
 * createVRMAnimationClip. Поэтому перемонтирование компонента или
 * второй экземпляр Юми не качают и не разбирают файлы заново.
 */
const parsed = new Map<string, Promise<VRMAnimation | null>>();

function parseVrma(url: string) {
  let job = parsed.get(url);
  if (!job) {
    job = (async () => {
      const loader = createGLTFLoader();
      loader.register((parser) => new VRMAnimationLoaderPlugin(parser));
      const gltf = await loader.parseAsync(await fetchBuffer(url), baseOf(url));
      const animations: VRMAnimation[] | undefined = gltf.userData.vrmAnimations;
      return animations?.[0] ?? null;
    })();
    // Неудачную загрузку не кешируем — следующая попытка пойдёт в сеть.
    // Удаляем только свою запись: после forgetClip там может лежать
    // уже новая загрузка того же файла.
    const mine = job;
    job.catch(() => { if (parsed.get(url) === mine) parsed.delete(url); });
    parsed.set(url, job);
  }
  return job;
}

/** Забыть разобранный файл, чтобы перечитать его с диска (песочница). */
export function forgetClip(url: string) {
  parsed.delete(url);
}

/** Грузит .vrma и привязывает к скелету модели. */
export async function loadClip(
  spec: ClipSpec, vrm: VRM,
): Promise<THREE.AnimationClip | null> {
  const animation = await parseVrma(spec.url);
  if (!animation) return null;
  return maskClip(createVRMAnimationClip(animation, vrm), vrm, spec.mask);
}

// ─── Маска: какие кости слушают клип ─────────────────────────

function maskClip(clip: THREE.AnimationClip, vrm: VRM, mask: ClipMask) {
  if (mask === 'none') return clip;
  const humanoid = vrm.humanoid;
  if (!humanoid) return clip;

  // Имя узла в сцене → имя гуманоидной кости, чтобы понять, что резать.
  const nodeToBone = new Map<string, string>();
  for (const bone of VRMHumanBoneList) {
    const node = humanoid.getNormalizedBoneNode(bone);
    if (node) nodeToBone.set(node.name, bone);
  }

  const kept = clip.tracks.filter((track) => {
    const dot = track.name.lastIndexOf('.');
    const bone = nodeToBone.get(track.name.slice(0, dot));
    // Не кость (мимика, взгляд) — маска её не касается.
    return !bone || keepTrack(bone, track.name.slice(dot + 1), mask);
  });

  return new THREE.AnimationClip(clip.name, clip.duration, kept);
}
