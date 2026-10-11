// Перенос правок из студии маскота (репозиторий mascot) в HoneyForge.
//
//   npm run mascot:sync -- [путь к mascot] [--prune]
//
// Копирует файлы данных, которые пишет лаборатория (specs/mascot.md, 4.8):
// пресеты, общий кадр, кадры клипов, переходы; фон окна Юми. Анимации копирует только те,
// на которые ссылаются состояния, пресеты и цепочки: HoneyForge не возит
// неиспользуемые клипы. С --prune удаляет из public/mascot/anim/vrma всё
// лишнее. Манифест анимаций пересобирается по оставшимся файлам.
// Проверку содержимого делает сам модуль при загрузке (битые записи
// отбрасываются), поэтому скрипт файлы не правит — только переносит.
import { copyFileSync, existsSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const prune = args.includes('--prune');
const source = resolve(args.find((a) => !a.startsWith('--')) ?? process.env.MASCOT_DIR ?? join(frontend, '..', '..', 'mascot'));

const from = (p) => join(source, p);
const to = (p) => join(frontend, p);
if (!existsSync(from('src/mascot/core/presets.json'))) {
  console.error(`Не найден модуль маскота в ${source}. Укажите путь: npm run mascot:sync -- <путь к mascot>`);
  process.exit(1);
}

// 1. Файлы данных студии.
const DATA = [
  'src/mascot/core/presets.json',
  'src/mascot/core/transitions.json',
  'src/mascot/renderers/vrm/camera.json',
  'src/mascot/renderers/vrm/clipCameras.json',
];
for (const file of DATA) {
  copyFileSync(from(file), to(file));
  console.log('данные:', file);
}

// 1б. Фон окна Юми — «комната с экранами» (specs/frontend.md, 6a).
if (existsSync(from('public/mascot/bg/room.webp'))) {
  copyFileSync(from('public/mascot/bg/room.webp'), to('public/mascot/bg/room.webp'));
  console.log('фон: public/mascot/bg/room.webp');
} else {
  console.warn('предупреждение: в студии нет public/mascot/bg/room.webp — фон окна Юми не обновлён');
}

// 2. Какие клипы нужны: действия встроенных состояний, пресетов и цепочек.
const used = new Set();
const addAction = (a) => { for (const name of Array.isArray(a) ? a : a ? [a] : []) used.add(name); };
const presets = JSON.parse(readFileSync(to('src/mascot/core/presets.json'), 'utf8'));
for (const spec of Object.values(presets)) addAction(spec?.action);
const transitions = JSON.parse(readFileSync(to('src/mascot/core/transitions.json'), 'utf8'));
for (const steps of Object.values(transitions?.chains ?? {})) for (const step of steps ?? []) addAction(step?.clip ?? step?.action);
const states = readFileSync(to('src/mascot/core/states.ts'), 'utf8');
for (const m of states.matchAll(/action:\s*(\[[^\]]*\]|'[^']*')/g)) for (const n of m[1].matchAll(/'([^']+)'/g)) used.add(n[1]);

// Имя клипа → файл по реестру; прямой путь (anim/vrma/...) берётся как есть.
const registry = new Map();
for (const m of readFileSync(to('src/mascot/core/clips.ts'), 'utf8').matchAll(/^\s+(\w+):\s+vrma\('([^']+)'/gm)) registry.set(m[1], m[2]);
// Танцы (состояние dance, `action: DANCES`) в HoneyForge намеренно не возим:
// таблица реакций их не вызывает, а меню «Станцевать» есть только в плавающем
// окне модуля, которого в HoneyForge нет (specs/frontend.md, раздел 9).
const files = new Set();
const unresolved = [];
for (const name of used) {
  if (registry.has(name)) files.add(registry.get(name));
  else if (name.startsWith('anim/vrma/')) {
    try { files.add(decodeURIComponent(name.slice('anim/vrma/'.length))); } catch { unresolved.push(name); }
  } else unresolved.push(name);
}
for (const name of unresolved) console.warn('клип не найден в реестре:', name);
// Реестр не разобрался (сменился формат clips.ts) или имя не найдено — список
// нужных клипов неполон, и --prune удалил бы нужное. Удалять не будем.
const pruneSafe = registry.size > 0 && unresolved.length === 0 && files.size > 0;
if (prune && !pruneSafe) {
  console.error('--prune отменён: список нужных клипов неполон (см. предупреждения выше). Ничего не удалено.');
  process.exitCode = 1;
}

// 3. Нужные анимации — из mascot; лишние — удалить (--prune).
const vrmaFrom = from('public/mascot/anim/vrma');
const vrmaTo = to('public/mascot/anim/vrma');
for (const file of files) {
  if (!existsSync(join(vrmaFrom, file))) { console.warn('нет файла в mascot:', file); continue; }
  copyFileSync(join(vrmaFrom, file), join(vrmaTo, file));
}
if (prune && pruneSafe) {
  for (const file of readdirSync(vrmaTo)) {
    // Удаляем только клипы; чужие файлы в папке не трогаем.
    if (!file.endsWith('.vrma') || files.has(file)) continue;
    unlinkSync(join(vrmaTo, file));
    // Синхронизируемые папки (OneDrive) могут вернуть файл — проверяем результат.
    if (existsSync(join(vrmaTo, file))) { console.error('не удалось удалить:', file); process.exitCode = 1; }
    else console.log('удалён лишний клип:', file);
  }
}

// 4. Манифест для лаборатории — по фактическим файлам.
const kept = readdirSync(vrmaTo).filter((f) => f.endsWith('.vrma')).sort();
const manifest = {
  files: kept.map((file) => ({
    file, group: 'vrma', rel: `vrma/${file}`, bytes: statSync(join(vrmaTo, file)).size,
    url: `/mascot/anim/vrma/${encodeURIComponent(file)}`,
  })),
};
writeFileSync(to('public/mascot/anim/manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`анимаций: ${kept.length} (нужно состояниям: ${files.size})`);
