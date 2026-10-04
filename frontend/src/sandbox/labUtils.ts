/** Раскодировать имя файла для подписи; битую кодировку показать как есть. */
export function fileLabel(path: string): string {
  const name = path.split('/').pop() ?? path;
  try { return decodeURIComponent(name); } catch { return name; }
}

/** JSON с ключами по алфавиту — сравнение не зависит от порядка ключей. */
export function stableJson(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) => (v && typeof v === 'object' && !Array.isArray(v)
    ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
    : v));
}
