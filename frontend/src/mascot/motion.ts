/**
 * Просил ли человек уменьшить движение (настройка ОС).
 * 'auto' — спросить систему, true/false — решение приложения.
 */
export function prefersReducedMotion(setting: boolean | 'auto' = 'auto'): boolean {
  if (setting !== 'auto') return setting;
  try {
    return globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  } catch {
    return false;
  }
}
