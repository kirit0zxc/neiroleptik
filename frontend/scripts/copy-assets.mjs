/**
 * Кладёт модель и клипы рядом со сборкой пакета.
 *
 *   node scripts/copy-assets.mjs dist-lib
 *
 * Получается <out>/mascot/{model,anim} — эту папку приложение копирует
 * к себе в статику и указывает путь к ней в assetsBase. Манифест
 * песочницы и служебные папки (_*, .*) не копируются.
 */
import { cpSync, rmSync } from 'node:fs';
import { basename, join } from 'node:path';

const out = process.argv[2];
if (!out) {
  console.error('укажите папку сборки: node scripts/copy-assets.mjs dist-lib');
  process.exit(1);
}

const target = join(out, 'mascot');
rmSync(target, { recursive: true, force: true });
cpSync('public/mascot', target, {
  recursive: true,
  filter: (src) => {
    const name = basename(src);
    return !name.startsWith('_') && !name.startsWith('.') && name !== 'manifest.json';
  },
});
console.log(`ассеты → ${target}`);
