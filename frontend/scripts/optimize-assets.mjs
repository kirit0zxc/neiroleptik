/**
 * Сжатие модели и клипов без поломки VRM.
 *
 *   node scripts/optimize-assets.mjs                  — модель + все .vrma
 *   node scripts/optimize-assets.mjs in.vrm out.vrm   — один файл
 *
 * Почему не gltf-transform: он выбрасывает незнакомые расширения,
 * а VRMC_vrm, springBone и MToon ссылаются на узлы, материалы и
 * текстуры по индексам. Здесь JSON не перестраивается — меняются
 * только байты внутри bufferView, поэтому все индексы остаются верны.
 *
 *  - текстуры → WebP (EXT_texture_webp); превью модели ужимается до 256px
 *  - геометрия, blendshape'ы, анимации → meshopt без потерь
 *    (EXT_meshopt_compression)
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import sharp from 'sharp';
import { MeshoptEncoder } from 'meshoptimizer';

const MODEL_SRC = 'assets-src/yumi.vrm';
const MODEL_OUT = 'public/mascot/model/yumi.vrm';
const ANIM_DIR = 'public/mascot/anim';

const MESHOPT = 'EXT_meshopt_compression';
const WEBP = 'EXT_texture_webp';
const ALIGN = 8;

const COMPONENT_BYTES = { 5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4 };
const TYPE_COUNT = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT2: 4, MAT3: 9, MAT4: 16 };
const elementSize = (a) => COMPONENT_BYTES[a.componentType] * TYPE_COUNT[a.type];
const align = (n, to = ALIGN) => Math.ceil(n / to) * to;

// ─── GLB ─────────────────────────────────────────────────────

function readGlb(file) {
  const buf = readFileSync(file);
  if (buf.readUInt32LE(0) !== 0x46546c67) throw new Error(`${file}: не GLB`);
  const jsonLen = buf.readUInt32LE(12);
  const json = JSON.parse(buf.subarray(20, 20 + jsonLen).toString('utf8'));
  const binStart = 20 + jsonLen;
  const bin = binStart < buf.length
    ? buf.subarray(binStart + 8, binStart + 8 + buf.readUInt32LE(binStart))
    : Buffer.alloc(0);
  return { json, bin };
}

function writeGlb(file, json, bin) {
  let jsonBuf = Buffer.from(JSON.stringify(json), 'utf8');
  jsonBuf = Buffer.concat([jsonBuf, Buffer.alloc(align(jsonBuf.length, 4) - jsonBuf.length, 0x20)]);
  const binBuf = Buffer.concat([bin, Buffer.alloc(align(bin.length, 4) - bin.length)]);
  const header = Buffer.alloc(12);
  header.writeUInt32LE(0x46546c67, 0);
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + 8 + jsonBuf.length + 8 + binBuf.length, 8);
  const chunk = (data, type) => {
    const h = Buffer.alloc(8);
    h.writeUInt32LE(data.length, 0);
    h.writeUInt32LE(type, 4);
    return [h, data];
  };
  writeFileSync(file, Buffer.concat([
    header, ...chunk(jsonBuf, 0x4e4f534a), ...chunk(binBuf, 0x004e4942),
  ]));
}

const addExtension = (json, name, required) => {
  json.extensionsUsed = [...new Set([...(json.extensionsUsed ?? []), name])];
  if (required) json.extensionsRequired = [...new Set([...(json.extensionsRequired ?? []), name])];
};

// ─── Текстуры ────────────────────────────────────────────────

/** Индекс картинки-превью: в рантайме она не нужна, хватит 256px. */
function thumbnailImage(json) {
  const v1 = json.extensions?.VRMC_vrm?.meta?.thumbnailImage;
  if (v1 != null) return v1;
  const v0tex = json.extensions?.VRM?.meta?.texture;
  return v0tex != null && v0tex >= 0 ? json.textures?.[v0tex]?.source : undefined;
}

async function encodeImage(data, { isNormal, isThumbnail }) {
  let img = sharp(data);
  if (isThumbnail) img = img.resize(256, 256, { fit: 'inside' });
  // Карты нормалей и мелкие служебные текстуры — без потерь:
  // артефакты сжатия на нормалях видны как рябь на освещении.
  const meta = await sharp(data).metadata();
  const lossless = isNormal || (meta.width ?? 0) * (meta.height ?? 0) <= 64 * 64;
  return img.webp(lossless ? { lossless: true, effort: 6 } : { quality: 90, alphaQuality: 100, effort: 6 }).toBuffer();
}

async function compressImages(json, views) {
  const thumb = thumbnailImage(json);
  const converted = new Set();
  for (const [index, image] of (json.images ?? []).entries()) {
    if (image.bufferView == null || image.mimeType === 'image/webp') continue;
    const view = views[image.bufferView];
    const isThumbnail = index === thumb;
    const isNormal = /nml|normal/i.test(image.name ?? '');
    const webp = await encodeImage(view.data, { isNormal, isThumbnail });
    // Иногда PNG уже меньше — тогда оставляем как есть.
    if (webp.length >= view.data.length && !isThumbnail) continue;
    view.data = webp;
    image.mimeType = 'image/webp';
    converted.add(index);
  }
  if (!converted.size) return;
  for (const texture of json.textures ?? []) {
    if (!converted.has(texture.source)) continue;
    texture.extensions = { ...texture.extensions, [WEBP]: { source: texture.source } };
    delete texture.source;
  }
  addExtension(json, WEBP, true);
}

// ─── Геометрия и анимации ────────────────────────────────────

/** Как сжимать каждый bufferView, или null — оставить как есть. */
function planMeshopt(json) {
  const usage = new Map();
  const markUse = (bv, kind, accessor) => {
    if (bv == null) return;
    const u = usage.get(bv) ?? { kinds: new Set(), accessors: [] };
    u.kinds.add(kind);
    if (accessor) u.accessors.push(accessor);
    usage.set(bv, u);
  };

  const indexAccessors = new Map();
  for (const mesh of json.meshes ?? []) {
    for (const p of mesh.primitives) {
      if (p.indices != null) indexAccessors.set(p.indices, (p.mode ?? 4) === 4);
    }
  }
  for (const [i, a] of (json.accessors ?? []).entries()) {
    markUse(a.bufferView, indexAccessors.has(i) ? 'index' : 'attr', a);
    if (a.sparse) {
      markUse(a.sparse.indices.bufferView, 'sparse');
      markUse(a.sparse.values.bufferView, 'sparse');
    }
  }
  for (const image of json.images ?? []) markUse(image.bufferView, 'image');

  const plan = new Map();
  for (const [bv, u] of usage) {
    if (u.kinds.size !== 1 || u.kinds.has('image') || u.kinds.has('sparse')) continue;
    const view = json.bufferViews[bv];
    const sizes = new Set(u.accessors.map(elementSize));
    if (sizes.size !== 1) continue;
    const size = [...sizes][0];

    if (u.kinds.has('index')) {
      if (size !== 2 && size !== 4) continue;
      const count = view.byteLength / size;
      const triangles = u.accessors.every((a) => indexAccessors.get(json.accessors.indexOf(a)));
      plan.set(bv, { mode: triangles && count % 3 === 0 ? 'TRIANGLES' : 'INDICES', stride: size, count });
      continue;
    }
    const stride = view.byteStride ?? size;
    if (stride % 4 !== 0 || stride > 256 || view.byteLength % stride !== 0) continue;
    plan.set(bv, { mode: 'ATTRIBUTES', stride, count: view.byteLength / stride });
  }
  return plan;
}

function compressGeometry(json, views) {
  const plan = planMeshopt(json);
  for (const [bv, p] of plan) {
    const view = views[bv];
    const encoded = MeshoptEncoder.encodeGltfBuffer(
      new Uint8Array(view.data.buffer, view.data.byteOffset, view.data.byteLength),
      p.count, p.stride, p.mode, 0,
    );
    if (encoded.length >= view.data.length) continue;
    view.meshopt = { ...p, data: Buffer.from(encoded) };
  }
}

// ─── Сборка ──────────────────────────────────────────────────

function rebuild(json, views) {
  const parts = [];
  let offset = 0;
  const place = (data) => {
    const at = align(offset);
    if (at > offset) parts.push(Buffer.alloc(at - offset));
    parts.push(data);
    offset = at + data.length;
    return at;
  };

  const anyMeshopt = views.some((v) => v.meshopt);
  // Несжатые данные лежат в fallback-буфере, у которого нет байтов:
  // загрузчик с декодером их не запрашивает.
  let fallbackLength = 0;
  const fallbackIndex = 1;

  json.bufferViews = views.map((v) => {
    const def = { ...v.def };
    if (v.meshopt) {
      def.buffer = fallbackIndex;
      def.byteOffset = align(fallbackLength);
      def.byteLength = v.meshopt.count * v.meshopt.stride;
      fallbackLength = def.byteOffset + def.byteLength;
      def.extensions = {
        ...def.extensions,
        [MESHOPT]: {
          buffer: 0,
          byteOffset: place(v.meshopt.data),
          byteLength: v.meshopt.data.length,
          byteStride: v.meshopt.stride,
          count: v.meshopt.count,
          mode: v.meshopt.mode,
        },
      };
    } else {
      def.buffer = 0;
      def.byteOffset = place(v.data);
      def.byteLength = v.data.length;
    }
    return def;
  });

  const bin = Buffer.concat(parts);
  json.buffers = [{ byteLength: bin.length }];
  if (anyMeshopt) {
    json.buffers.push({ byteLength: fallbackLength, extensions: { [MESHOPT]: { fallback: true } } });
    addExtension(json, MESHOPT, true);
  }
  return bin;
}

async function optimize(input, output) {
  const { json, bin } = readGlb(input);
  if (json.extensionsUsed?.includes(MESHOPT)) {
    console.log(`  пропуск, уже сжат: ${input}`);
    return;
  }
  if ((json.buffers ?? []).length > 1 || json.buffers?.[0]?.uri) {
    throw new Error(`${input}: ожидался один встроенный буфер`);
  }

  const views = (json.bufferViews ?? []).map((def) => ({
    def,
    data: bin.subarray(def.byteOffset ?? 0, (def.byteOffset ?? 0) + def.byteLength),
  }));

  await compressImages(json, views);
  compressGeometry(json, views);
  const outBin = rebuild(json, views);
  // Размер замеряем до записи: файл может сжиматься на месте.
  const before = statSync(input).size;
  writeGlb(output, json, outBin);
  const after = statSync(output).size;
  console.log(`  ${output}: ${kb(before)} → ${kb(after)} (−${Math.round((1 - after / before) * 100)}%)`);
}

const kb = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(2)} МБ` : `${Math.round(n / 1024)} КБ`);

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    if (name.startsWith('_') || name.startsWith('.')) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (name.toLowerCase().endsWith('.vrma')) yield full;
  }
}

await MeshoptEncoder.ready;
const [input, output] = process.argv.slice(2);
if (input) {
  await optimize(input, output ?? input);
} else {
  console.log('Модель:');
  await optimize(MODEL_SRC, MODEL_OUT);
  console.log('Клипы (на месте, без потерь):');
  for (const file of walk(ANIM_DIR)) await optimize(file, file);
}
