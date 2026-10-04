/** Раскодировать имя файла для подписи; битую кодировку показать как есть. */
export function fileLabel(path: string): string {
  const name = path.split('/').pop() ?? path;
  try { return decodeURIComponent(name); } catch { return name; }
}
