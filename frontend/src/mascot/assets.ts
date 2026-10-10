/**
 * Метка, которой мост помечает свою обёртку над fetch: по ней
 * загрузчик находит настоящий fetch под ней.
 */
export const ORIGINAL_FETCH = Symbol.for('mascot.originalFetch');

type Wrapped = typeof fetch & { [ORIGINAL_FETCH]?: typeof fetch };

/**
 * fetch, сохранённый при загрузке модуля.
 *
 * Мост умеет оборачивать window.fetch, чтобы запросы приложения двигали
 * Юми. Загрузка её собственной модели и клипов через ту же обёртку
 * заставила бы её «думать» и радоваться самой себе на старте.
 */
const captured: Wrapped = globalThis.fetch;

/** Снять обёртки моста — на случай, если fetch обернули раньше, чем загрузился этот модуль. */
function rawFetch(): typeof fetch {
  let f = captured;
  while (f[ORIGINAL_FETCH]) f = f[ORIGINAL_FETCH] as Wrapped;
  return f;
}

export async function fetchBuffer(url: string): Promise<ArrayBuffer> {
  // Нативный fetch требует this === window, иначе Illegal invocation.
  const res = await rawFetch().call(globalThis, url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${url}`);
  return res.arrayBuffer();
}

/** Папка файла — от неё GLTF ищет внешние текстуры и буферы. */
export const baseOf = (url: string) => url.slice(0, url.lastIndexOf('/') + 1);

/** Абсолютный путь или URL со схемой (http:, blob:, data:, chrome-extension: ...). */
const ABSOLUTE = /^(?:[a-z][a-z\d+.-]*:|\/)/i;

/**
 * Собрать адрес ассета.
 *
 * Относительные пути считаются от assetsBase, поэтому приложение,
 * живущее не в корне домена, или расширение (chrome.runtime.getURL)
 * меняют одну настройку, а не реестр клипов.
 */
export function resolveAsset(url: string, base: string): string {
  if (ABSOLUTE.test(url)) return url;
  return (base.endsWith('/') ? base : base + '/') + url;
}
