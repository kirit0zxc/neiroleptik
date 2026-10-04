import { useEffect, useMemo, useState } from 'react';
import { Mascot } from '../mascot/react/Mascot';
import { useMascot } from '../mascot/react/useMascot';
import { STATES } from '../mascot/core/states';
import { Studio, PREVIEW_STATE } from './Studio';
import { CameraPanel } from './CameraPanel';
import { Sequence } from './Sequence';
import { CLIPS, type ClipMask } from '../mascot/core/clips';
import { BrowserTTS, type VoiceInfo } from '../mascot/voice/BrowserTTS';
import type { MascotState } from '../mascot/core/types';
import './sandbox.css';

const CLIP_NAMES = Object.keys(CLIPS);

interface AnimFile {
  file: string;
  group: string;
  rel: string;
  url: string;
  bytes: number;
  mtime: number;
}

export function Sandbox() {
  const { state, ready, bubble, speaking, set, say, hush, setVoiceOptions, core } = useMascot();
  const [phrase, setPhrase] = useState('Привет! Я Юми, помогу с моделью.');
  const [files, setFiles] = useState<AnimFile[]>([]);
  const [misplaced, setMisplaced] = useState<AnimFile[]>([]);
  const [mask, setMask] = useState<ClipMask>('inplace');
  const [playing, setPlaying] = useState<string | null>(null);
  const [spread, setSpread] = useState(8);
  const [voices, setVoices] = useState<VoiceInfo[] | null>(null);
  const [voiceName, setVoiceName] = useState<string>('');
  const [pitch, setPitch] = useState(1.25);
  const [speed, setSpeed] = useState(0.95);
  // Студия добавляет и удаляет состояния — кнопки пересчитываются.
  const [statesVersion, setStatesVersion] = useState(0);
  const [tab, setTab] = useState<'states' | 'camera' | 'sequence'>('states');
  const [stage, setStage] = useState<HTMLElement | null>(null);
  const stateNames = useMemo(
    () => (Object.keys(STATES) as MascotState[]).filter((n) => !n.startsWith('__')),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [statesVersion],
  );

  // Список голосов приходит асинхронно — спрашиваем один раз на старте.
  useEffect(() => {
    void BrowserTTS.list().then((list) => {
      setVoices(list);
      // Предвыбираем тот же голос, которым Юми заговорит сама:
      // Ирину, а без неё — лучший из русских.
      const best = BrowserTTS.choose(list.map((v) => ({ ...v, default: v.isDefault })));
      if (best) setVoiceName(best.name);
    });
  }, []);

  useEffect(() => {
    setVoiceOptions({ voiceName: voiceName || undefined, pitch, rate: speed });
  }, [voiceName, pitch, speed, setVoiceOptions]);

  // Разведение плеч крутим на лету: угол подбирается только глазами.
  useEffect(() => {
    (core.getRenderer() as any)?.setArmSpread?.(spread);
  }, [spread, ready, core]);

  // Список файлов отдаёт дев-сервер, сканируя папку на каждый запрос.
  useEffect(() => { void refresh(); }, []);

  async function refresh() {
    try {
      const res = await fetch('/mascot/anim/manifest.json', { cache: 'no-store' });
      const data = await res.json();
      setFiles(data.files ?? data ?? []);
      setMisplaced(data.misplaced ?? []);
    } catch {
      setFiles([]);
      setMisplaced([]);
    }
  }

  /** Перенести файлы, случайно положенные в dist, туда где их читают. */
  async function syncMisplaced() {
    await fetch('/mascot/anim/sync', { method: 'POST', cache: 'no-store' });
    await refresh();
    // Разобранные клипы держатся в памяти — сбрасываем, иначе
    // проиграется то, что было до подмены.
    const r = core.getRenderer() as any;
    for (const f of files) r?.forgetFile?.(f.url);
  }

  function playFile(f: AnimFile) {
    const r = core.getRenderer() as any;
    if (!r?.playFile) return;
    // Сбрасываем разобранный клип, иначе после подмены файла
    // проиграется то, что осталось в памяти с прошлого раза.
    r.forgetFile?.(f.url);
    r.playFile(f.url, { mask, loop: true });
    setPlaying(f.rel);
  }

  return (
    <div className="sb">
      <aside className="sb__panel">
        <header className="sb__head">
          <h1>Юми</h1>
          <span className={ready ? 'sb__dot sb__dot--on' : 'sb__dot'}>
            {ready ? 'готова' : 'грузится…'}
          </span>
          <a className="sb__link sb__link--right" href="./">← окно</a>
        </header>

        <section>
          <h2>Состояния пайплайна</h2>
          <div className="sb__grid">
            {stateNames.map((name) => (
              <button
                key={name}
                className={name === state ? 'sb__btn sb__btn--active' : 'sb__btn'}
                onClick={() => { setPlaying(null); set(name); }}
              >
                {name}
              </button>
            ))}
          </div>
        </section>

        <section>
          <h2>Жесты из реестра</h2>
          <div className="sb__grid">
            {CLIP_NAMES.map((name) => (
              <button
                key={name}
                className="sb__btn sb__btn--ghost"
                onClick={() => {
                  setPlaying(null);
                  core.getRenderer()?.playClip(name, { loop: false, fadeMs: 250 });
                }}
              >
                {name}
              </button>
            ))}
          </div>
        </section>

        <section>
          <div className="sb__row">
            <h2>Файлы в папке — {files.length}</h2>
            <button className="sb__link" onClick={() => void refresh()}>обновить</button>
          </div>

          {misplaced.length > 0 && (
            <div className="sb__warn">
              <b>{misplaced.length} файл(ов) лежат в dist/</b>
              <span>
                Это каталог сборки, дев-сервер его не отдаёт — поэтому
                анимации и не меняются. Класть надо в public/.
              </span>
              <button className="sb__btn sb__btn--wide" onClick={() => void syncMisplaced()}>
                перенести в public
              </button>
            </div>
          )}

          <div className="sb__masks">
            {(['inplace', 'none', 'legs', 'strict'] as ClipMask[]).map((m) => (
              <button
                key={m}
                className={m === mask ? 'sb__chip sb__chip--on' : 'sb__chip'}
                onClick={() => setMask(m)}
                title={
                  m === 'inplace' ? 'всё тело, но с места не сходит'
                  : m === 'none' ? 'как есть, вместе с корневым движением'
                  : m === 'legs' ? 'без ног, таз оставлен'
                  : 'только верх тела'
                }
              >
                {m}
              </button>
            ))}
          </div>

          {groupBy(files).map(([group, items]) => (
            <div key={group} className="sb__group">
              <div className="sb__group-head">{group || 'корень anim/'}</div>
              <div className="sb__files">
                {items.map((f) => (
                  <button
                    key={f.url}
                    className={f.rel === playing ? 'sb__file sb__file--on' : 'sb__file'}
                    onClick={() => playFile(f)}
                  >
                    <span className="sb__file-name">{f.file}</span>
                    <span className="sb__file-meta">
                      {fmtSize(f.bytes)} · {timeOf(f.mtime)}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}
          {!files.length && <p className="sb__hint">Файлов не найдено.</p>}
        </section>

        <section>
          <div className="sb__row">
            <h2>Плечи наружу</h2>
            <span className="sb__val">{spread}°</span>
          </div>
          <input
            className="sb__range"
            type="range" min={0} max={25} step={1}
            value={spread}
            onChange={(e) => setSpread(Number(e.target.value))}
          />
          <p className="sb__hint">
            Худи шире торса — без отвода руки уходят в ткань.
          </p>
        </section>

        <section>
          <h2>Реплика</h2>
          <textarea
            className="sb__input"
            value={phrase}
            rows={2}
            onChange={(e) => setPhrase(e.target.value)}
          />
          {voices && <VoicePanel voices={voices} />}

          {voices && voices.length > 0 && (
            <>
              <select
                className="sb__select"
                value={voiceName}
                onChange={(e) => setVoiceName(e.target.value)}
              >
                {voices.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name} [{v.lang}]{v.local ? '' : ' · сеть'}
                  </option>
                ))}
              </select>

              <div className="sb__row">
                <span className="sb__knob">высота</span>
                <span className="sb__val">{pitch.toFixed(2)}</span>
              </div>
              <input
                className="sb__range" type="range"
                min={0.6} max={2} step={0.05}
                value={pitch}
                onChange={(e) => setPitch(Number(e.target.value))}
              />

              <div className="sb__row">
                <span className="sb__knob">темп</span>
                <span className="sb__val">{speed.toFixed(2)}</span>
              </div>
              <input
                className="sb__range" type="range"
                min={0.6} max={1.6} step={0.05}
                value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
              />
            </>
          )}

          <div className="sb__sayrow">
            <button className="sb__btn sb__btn--wide" onClick={() => say(phrase)}>
              {speaking ? 'говорит…' : 'сказать'}
            </button>
            {speaking && (
              <button className="sb__btn" onClick={hush}>стоп</button>
            )}
          </div>
        </section>

        <p className="sb__hint">Двигай курсором — Юми следит глазами и головой.</p>
      </aside>

      <main className="sb__stage" ref={setStage}>
        <Mascot className="sb__view" />
        {bubble && <div className="sb__bubble">{bubble}</div>}
        {speaking && <div className="sb__speaking">говорит</div>}
        <div className="sb__state">{playing ?? (state === PREVIEW_STATE ? 'студия' : state)}</div>
      </main>

      <aside className="sb__panel sb__studio">
        <header className="sb__tabs">
          <button
            className={tab === 'states' ? 'sb__tab sb__tab--on' : 'sb__tab'}
            onClick={() => setTab('states')}
          >
            Состояния
          </button>
          <button
            className={tab === 'camera' ? 'sb__tab sb__tab--on' : 'sb__tab'}
            onClick={() => setTab('camera')}
          >
            Камера
          </button>
          <button
            className={tab === 'sequence' ? 'sb__tab sb__tab--on' : 'sb__tab'}
            onClick={() => setTab('sequence')}
          >
            Цепочка
          </button>
        </header>
        {/* Обе вкладки остаются смонтированными: черновик студии не теряется. */}
        <div hidden={tab !== 'states'}>
          <Studio core={core} files={files} onStatesChange={() => setStatesVersion((v) => v + 1)} />
        </div>
        <div hidden={tab !== 'camera'}>
          <CameraPanel core={core} ready={ready} stage={stage} />
        </div>
        <div hidden={tab !== 'sequence'}>
          <Sequence core={core} stateNames={stateNames} files={files} />
        </div>
      </aside>
    </div>
  );
}

/** Какие голоса есть в системе и выберется ли русский. */
function VoicePanel({ voices }: { voices: VoiceInfo[] }) {
  const ru = voices.filter((v) => v.lang.toLowerCase().startsWith('ru'));

  if (!voices.length) {
    return (
      <div className="sb__warn">
        <b>Голосов не найдено</b>
        <span>
          Браузер не отдал ни одного голоса. Проверь, что вкладка не
          в фоне, и попробуй перезагрузить страницу.
        </span>
      </div>
    );
  }

  if (!ru.length) {
    return (
      <div className="sb__warn">
        <b>Русского голоса нет</b>
        <span>
          Доступно голосов: {voices.length}, но ни одного с языком ru.
          Юми прочитает русский текст чужим голосом — звучать будет
          странно. Установи русский голос в параметрах Windows.
        </span>
      </div>
    );
  }

  return (
    <p className="sb__hint">
      Русских голосов: {ru.length} — {ru.map((v) => v.name).join(', ')}
    </p>
  );
}

/** Раскладывает файлы по подпапкам, корень идёт первым. */
function groupBy(files: AnimFile[]): [string, AnimFile[]][] {
  const map = new Map<string, AnimFile[]>();
  for (const f of files) {
    const arr = map.get(f.group) ?? [];
    arr.push(f);
    map.set(f.group, arr);
  }
  return [...map.entries()].sort(([a], [b]) =>
    a === '' ? -1 : b === '' ? 1 : a.localeCompare(b, 'ru'));
}

function fmtSize(bytes: number) {
  return bytes >= 1048576
    ? `${(bytes / 1048576).toFixed(1)} МБ`
    : `${Math.round(bytes / 1024)} КБ`;
}

function timeOf(ms: number) {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
