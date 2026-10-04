import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { readdirSync, writeFileSync, mkdirSync, statSync, copyFileSync } from 'node:fs'
import { join, relative, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parsePresets } from './src/mascot/core/presets.ts'
import { parseTransitionsFile } from './src/mascot/core/transitions.ts'
import { cameraDiff, parseCamera, CAMERA_DEFAULTS } from './src/mascot/renderers/vrm/camera.ts'
import { parseClipCameras } from './src/mascot/renderers/vrm/clipCameras.ts'

/** Папка фронтенда: пути не зависят от того, откуда запущен Vite. */
const ROOT = dirname(fileURLToPath(import.meta.url))
const PUB = join(ROOT, 'public/mascot/anim')
const DIST = join(ROOT, 'dist/mascot/anim')

type Entry = {
  file: string          // имя файла
  group: string         // подпапка, '' для корня
  rel: string           // путь относительно anim/
  url: string
  bytes: number
  mtime: number
}


/**
 * Обходит всё дерево под anim/, а не две фиксированные папки.
 *
 * Так можно раскладывать анимации как удобно — по источнику,
 * по состояниям, как угодно, — и они появятся в песочнице сами.
 * Папки, начинающиеся с подчёркивания, пропускаем: там запасные копии.
 */
function walk(root: string, dir = root, out: Entry[] = [], depth = 0): Entry[] {
  if (depth > 4) return out
  let names: string[] = []
  try { names = readdirSync(dir) } catch { return out }

  for (const name of names) {
    if (name.startsWith('_') || name.startsWith('.')) continue
    const full = join(dir, name)
    let s
    try { s = statSync(full) } catch { continue }

    if (s.isDirectory()) { walk(root, full, out, depth + 1); continue }

    if (!name.toLowerCase().endsWith('.vrma')) continue
    const rel = relative(root, full).split('\\').join('/')
    const group = dirname(rel) === '.' ? '' : dirname(rel)
    out.push({
      file: name, group, rel, bytes: s.size, mtime: s.mtimeMs,
      url: '/mascot/anim/' + rel.split('/').map(encodeURIComponent).join('/'),
    })
  }
  return out
}

const list = (root: string) =>
  walk(root).sort((a, b) =>
    a.group.localeCompare(b.group, 'ru') || a.file.localeCompare(b.file, 'ru'))

/** Файлы в dist, которые новее своих двойников в public. */
function findMisplaced() {
  const pub = new Map(list(PUB).map((e) => [e.rel, e]))
  return list(DIST).filter((d) => {
    const p = pub.get(d.rel)
    return !p || d.mtime > p.mtime + 1000
  })
}

function syncMisplaced(): string[] {
  const moved: string[] = []
  for (const m of findMisplaced()) {
    try {
      mkdirSync(dirname(join(PUB, m.rel)), { recursive: true })
      copyFileSync(join(DIST, m.rel), join(PUB, m.rel))
      moved.push(m.rel)
    } catch { /* пропускаем недоступные */ }
  }
  return moved
}

/**
 * Запрос на запись пришёл со страницы самого dev-сервера.
 *
 * Без проверки любая открытая вкладка могла бы простым POST (text/plain
 * не требует CORS-предзапроса) перезаписать пресеты, камеру или
 * таблицу переходов — а они попадают в сборку.
 */
function sameOrigin(req: IncomingMessage): boolean {
  const origin = req.headers.origin
  const host = req.headers.host
  if (!origin || !host) return false
  try { return new URL(origin).host === host } catch { return false }
}

/** Ответить отказом и закончить. */
function deny(res: ServerResponse, status: number, message: string) {
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json')
  res.end(JSON.stringify({ error: message }))
}

/** Больше — точно не пресеты: защита от случайной или злой огромной записи. */
const MAX_BODY = 1024 * 1024

/** Ошибка запроса с HTTP-статусом: тело слишком большое, не JSON и т. п. */
class RequestError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/**
 * Прочитать тело целиком. Куски склеиваются как байты, а не строки:
 * иначе кириллический символ на границе куска превращался в «�», и
 * пресет с таким именем молча пропадал при проверке.
 */
function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    req.on('data', (chunk: Buffer) => {
      size += chunk.length
      // Отвечаем 413 и перестаём копить тело; соединение закроется после ответа.
      if (size > MAX_BODY) { reject(new RequestError(413, 'слишком большое тело запроса')); req.pause(); return }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function animManifest(): Plugin {
  return {
    name: 'anim-manifest',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url ?? ''
        if (url.startsWith('/mascot/anim/sync')) {
          // Копирует файлы в public — только POST и только со своей страницы.
          if (req.method !== 'POST') return deny(res, 405, 'только POST')
          if (!sameOrigin(req)) return deny(res, 403, 'запрос не со страницы песочницы')
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ moved: syncMisplaced() }))
          return
        }
        if (!url.startsWith('/mascot/anim/manifest.json')) return next()
        res.setHeader('Content-Type', 'application/json')
        res.setHeader('Cache-Control', 'no-store')
        res.end(JSON.stringify({ files: list(PUB), misplaced: findMisplaced() }))
      })
    },
    buildStart() {
      mkdirSync(PUB, { recursive: true })
      writeFileSync(join(PUB, 'manifest.json'),
        JSON.stringify({ files: list(PUB), misplaced: [] }, null, 2))
    },
  }
}

const PRESETS_FILE = resolve(ROOT, 'src/mascot/core/presets.json')
const CAMERA_FILE = resolve(ROOT, 'src/mascot/renderers/vrm/camera.json')
const TRANSITIONS_FILE = resolve(ROOT, 'src/mascot/core/transitions.json')
const CLIP_CAMERAS_FILE = resolve(ROOT, 'src/mascot/renderers/vrm/clipCameras.json')

/**
 * Сохранение из песочницы: пресеты студии и кадр по умолчанию.
 *
 * POST с полным содержимым — файл перезаписывается целиком. Данные
 * проходят через тот же разбор, что и рантайм, поэтому в файл не
 * попадёт ничего, что маскот не сможет прочитать.
 */
const SAVES: { route: string; file: string; parse: (raw: unknown) => object }[] = [
  { route: '/mascot/presets', file: PRESETS_FILE, parse: parsePresets },
  { route: '/mascot/transitions', file: TRANSITIONS_FILE, parse: parseTransitionsFile },
  { route: '/mascot/clip-cameras', file: CLIP_CAMERAS_FILE, parse: parseClipCameras },
  {
    route: '/mascot/camera',
    file: CAMERA_FILE,
    // В файл — только отличия от встроенного кадра.
    parse: (raw) => cameraDiff({ ...CAMERA_DEFAULTS, ...parseCamera(raw) }),
  },
]

function sandboxSaves(): Plugin {
  return {
    name: 'sandbox-saves',
    configureServer(server) {
      for (const { route, file, parse } of SAVES) {
        server.middlewares.use(route, (req, res) => {
          if (req.method !== 'POST') return deny(res, 405, 'только POST')
          if (!sameOrigin(req)) return deny(res, 403, 'запрос не со страницы песочницы')
          // JSON-заголовок: чужой сайт не отправит его без CORS-предзапроса.
          if (!String(req.headers['content-type']).startsWith('application/json')) {
            return deny(res, 415, 'нужен Content-Type: application/json')
          }
          readBody(req).then((body) => {
            let raw: unknown
            try { raw = JSON.parse(body) } catch { throw new RequestError(400, 'тело — не JSON') }
            // null, массив или строка разобрались бы в пустой объект и молча
            // затёрли бы весь файл данных.
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
              throw new RequestError(400, 'нужен JSON-объект')
            }
            const data = parse(raw)
            try {
              writeFileSync(file, JSON.stringify(data, null, 2) + '\n')
            } catch (error) {
              throw new RequestError(500, 'не удалось записать файл: ' + String(error))
            }
            res.setHeader('Content-Type', 'application/json')
            res.end(JSON.stringify({ saved: data }))
          }).catch((error) => {
            if (res.headersSent || res.writableEnded) return
            res.setHeader('Connection', 'close')
            deny(res, error instanceof RequestError ? error.status : 500, error instanceof Error ? error.message : String(error))
          })
        })
      }
    },
    // Песочница уже держит сохранённое в памяти — перезагрузка
    // страницы на каждое сохранение сбросила бы то, что на экране.
    // Следствие: и ручная правка этих файлов в редакторе видна только
    // после перезагрузки страницы.
    handleHotUpdate({ file }) {
      if (SAVES.some((s) => s.file === resolve(file))) return []
    },
  }
}

export default defineConfig({ plugins: [react(), animManifest(), sandboxSaves()] })
