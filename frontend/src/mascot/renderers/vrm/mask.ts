import type { ClipMask } from '../../core/clips';

/** Кости ниже позвоночника — их отбрасываем для портретного кадра. */
const LOWER_BODY = new Set([
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes',
  'rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes',
]);

/**
 * Пропускать ли дорожку при заданной маске.
 *
 * Юми всегда в кадре по грудь, ноги не видны никогда. Выбрасывая их,
 * мы и экономим, и снимаем конфликт: низ остаётся за процедурной
 * анимацией, а клип управляет только тем, что видно.
 */
export function keepTrack(
  vrmBoneName: string,
  property: string,
  mask: ClipMask,
): boolean {
  if (mask === 'none') return true;

  // inplace: тело живёт целиком, но корень не едет.
  if (mask === 'inplace') {
    return !(vrmBoneName === 'hips' && property === 'position');
  }

  if (LOWER_BODY.has(vrmBoneName)) return false;

  if (vrmBoneName === 'hips') {
    // Смещение таза увело бы Юми из кадра — режем всегда.
    if (property === 'position') return false;
    // В strict убираем и поворот: клипы, снятые сидя, уводят
    // таз в посадку и заваливают весь корпус.
    if (mask === 'strict') return false;
  }
  return true;
}
