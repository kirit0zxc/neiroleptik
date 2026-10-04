import { useEffect, useRef, useState } from 'react';
import type { MascotCore } from '../mascot/core/MascotCore';
import { DEFAULT_FADE_MS } from '../mascot/core/MascotCore';
import { CLIPS } from '../mascot/core/clips';
import { BUILTIN_STATES, PRESETS, STATES } from '../mascot/core/states';
import { CHAINS, TRANSITIONS, defineTransition, removeTransition } from '../mascot/core/transitionTable';
import {
  isChainName, splitTransitionKey, transitionKey, type ChainStep, type TransitionsFile,
} from '../mascot/core/transitions';
import type { VrmRenderer } from '../mascot/renderers/vrm/VrmRenderer';

type Step = ChainStep & { id: number };

const STORAGE_KEY = 'mascot.sandbox.sequence';

/** Черновик цепочки живёт в этом браузере; сохранённые — в transitions.json. */
function loadDraft(): Step[] {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(raw) ? raw.filter((s) => s && typeof s.name === 'string') : [];
  } catch {
    return [];
  }
}

function saveDraft(steps: Step[]) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(steps)); } catch { /* приватный режим */ }
}

/** Пауза, которую можно прервать кнопкой «стоп». */
const sleep = (ms: number, run: { cancelled: boolean }) => new Promise<void>((resolve) => {
  const started = performance.now();
  const tick = () => {
    if (run.cancelled || performance.now() - started >= ms) resolve();
    else setTimeout(tick, Math.min(50, ms));
  };
  tick();
});

const label = (name: string) => decodeURIComponent(name.split('/').pop() ?? name);
const withIds = (steps: ChainStep[]): Step[] => steps.map((s, i) => ({ ...s, id: Date.now() + i }));
const withoutIds = (steps: Step[]): ChainStep[] => steps.map(({ id: _id, ...s }) => s);

type FadeSource = 'шаг' | 'таблица' | 'состояние' | 'обычный';

interface SequenceProps {
  core: MascotCore;
  stateNames: string[];
  files: { rel: string; file: string }[];
}

/**
 * Цепочка: несколько состояний или клипов подряд — чтобы смотреть и
 * подбирать переходы. Удачные переходы уходят в таблицу переходов:
 * её применяет ядро, и новые цепочки берут значения из неё.
 */
export function Sequence({ core, stateNames, files }: SequenceProps) {
  const [steps, setSteps] = useState<Step[]>(loadDraft);
  const [table, setTable] = useState<Record<string, number>>(() => ({ ...TRANSITIONS }));
  const [chains, setChains] = useState<Record<string, ChainStep[]>>(() => ({ ...CHAINS }));
  const [pick, setPick] = useState('');
  const [chainName, setChainName] = useState('');
  const [active, setActive] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);
  const [overlap, setOverlap] = useState(true);
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const run = useRef<{ cancelled: boolean } | null>(null);
  // Плеер читает актуальное, даже если цепочку правят на ходу.
  const live = useRef({ steps, loop, overlap, table });
  useEffect(() => { live.current = { steps, loop, overlap, table }; }, [steps, loop, overlap, table]);

  useEffect(() => { saveDraft(steps); }, [steps]);
  useEffect(() => () => { if (run.current) run.current.cancelled = true; }, []);

  const renderer = () => core.getRenderer() as VrmRenderer | null;

  /** Шаг перед i с учётом повтора по кругу. */
  const prevOf = (list: Step[], i: number, looped: boolean) =>
    i > 0 ? list[i - 1] : looped && list.length > 1 ? list[list.length - 1] : null;

  /** Чем вливаемся в шаг и откуда взялось значение. */
  function fadeOf(step: Step, prev: Step | null, t = table): { ms: number; source: FadeSource } {
    if (step.fadeMs != null) return { ms: step.fadeMs, source: 'шаг' };
    const fromTable = prev ? t[transitionKey(prev.name, step.name)] : undefined;
    if (fromTable != null) return { ms: fromTable, source: 'таблица' };
    const own = step.kind === 'state' ? STATES[step.name]?.fadeMs : undefined;
    if (own != null) return { ms: own, source: 'состояние' };
    return { ms: DEFAULT_FADE_MS, source: 'обычный' };
  }

  const update = (id: number, patch: Partial<Step>) =>
    setSteps((list) => list.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  const move = (i: number, by: number) => setSteps((list) => {
    const j = i + by;
    if (j < 0 || j >= list.length) return list;
    const next = [...list];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });

  function add() {
    if (!pick) return;
    // value вида state:имя или clip:путь — путь сам может содержать двоеточие.
    const at = pick.indexOf(':');
    const kind = pick.slice(0, at) as Step['kind'];
    const name = pick.slice(at + 1);
    setSteps((list) => [...list, { id: Date.now(), kind, name, mode: 'end', secs: 3, fadeMs: null }]);
  }

  // ─── Проигрывание ─────────────────────────────────────────

  /** Дождаться, пока клип станет текущим, и узнать, где он сейчас. */
  async function waitClip(clip: string | null, r: { cancelled: boolean }) {
    if (!clip) return null;
    for (let i = 0; i < 200 && !r.cancelled; i++) {
      const st = renderer()?.getClipState?.();
      if (st?.name === clip) return st;
      await sleep(50, r);
    }
    return null;
  }

  async function play() {
    if (run.current) run.current.cancelled = true;
    const r = { cancelled: false };
    run.current = r;
    setPlaying(true);
    let first = true;

    do {
      const list = live.current.steps;
      for (let i = 0; i < list.length && !r.cancelled; i++) {
        const step = live.current.steps.find((s) => s.id === list[i].id) ?? list[i];
        // У самого первого шага «предыдущего» нет — Юми входит из того, что было.
        const prev = first && i === 0 ? null : prevOf(list, i, live.current.loop);
        first = false;
        setActive(i);
        const fade = fadeOf(step, prev, live.current.table).ms;

        let clip: string | null = null;
        if (step.kind === 'state') {
          // Плавность передаём явно: перед шагом мог быть клип, а не
          // состояние, и сама таблица ядра пару бы не нашла.
          const off = core.on((e) => { if (e.type === 'clip') clip = e.name; });
          core.setState(step.name, { fadeMs: fade });
          off();
        } else {
          clip = step.name;
          renderer()?.playClip(step.name, { loop: step.mode === 'secs', fadeMs: fade });
        }

        let ms: number;
        if (step.mode === 'secs') {
          ms = step.secs * 1000;
        } else {
          const st = await waitClip(clip, r);
          // end — конец отрезка: у обрезанного клипа он раньше конца файла.
          ms = st ? Math.max(0, st.end - st.time) * 1000 : 2000;
        }

        // Перехлёст: следующий шаг вливается, пока этот ещё двигается, —
        // иначе переход идёт из застывшей последней позы.
        const nextIndex = i + 1 < list.length ? i + 1 : live.current.loop ? 0 : -1;
        if (live.current.overlap && nextIndex >= 0 && step.mode === 'end') {
          ms -= fadeOf(list[nextIndex], step, live.current.table).ms;
        }
        await sleep(Math.max(ms, 50), r);
      }
    } while (live.current.loop && !r.cancelled && live.current.steps.length);

    if (run.current === r) {
      run.current = null;
      setPlaying(false);
      setActive(-1);
    }
  }

  function stop() {
    if (run.current) run.current.cancelled = true;
    run.current = null;
    setPlaying(false);
    setActive(-1);
    core.setState('idle');
  }

  // ─── Сохранение ───────────────────────────────────────────

  async function persist(next: TransitionsFile, message: string) {
    try {
      const res = await fetch('/mascot/transitions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setTable(next.transitions);
      setChains(next.chains);
      setStatus({ ok: true, text: message });
      return true;
    } catch {
      setStatus({ ok: false, text: 'Не сохранилось: запись в файл работает только из npm run dev.' });
      return false;
    }
  }

  /** Переходы, подкрученные у шагов, — в таблицу. */
  async function saveTransitions() {
    const next = { ...table };
    const saved: number[] = [];
    steps.forEach((step, i) => {
      const prev = prevOf(steps, i, loop);
      if (step.fadeMs == null || !prev) return;
      next[transitionKey(prev.name, step.name)] = step.fadeMs;
      saved.push(step.id);
    });
    if (!saved.length) {
      setStatus({ ok: false, text: 'Нечего сохранять: подкрутите «переход в шаг» хотя бы у одного шага, кроме первого.' });
      return;
    }
    if (!(await persist({ transitions: next, chains }, `В таблицу переходов: ${saved.length}`))) return;
    for (const key of Object.keys(next)) {
      const [from, to] = splitTransitionKey(key);
      defineTransition(from, to, next[key]);
    }
    // Значения теперь в таблице — шаги берут их оттуда.
    setSteps((list) => list.map((s) => (saved.includes(s.id) ? { ...s, fadeMs: null } : s)));
  }

  async function deleteTransition(key: string) {
    const next = { ...table };
    delete next[key];
    if (!(await persist({ transitions: next, chains }, 'Переход удалён'))) return;
    const [from, to] = splitTransitionKey(key);
    removeTransition(from, to);
  }

  async function saveChain() {
    const name = chainName.trim();
    if (!isChainName(name) || !steps.length) return;
    await persist({ transitions: table, chains: { ...chains, [name]: withoutIds(steps) } }, `Цепочка сохранена: ${name}`);
  }

  async function deleteChain(name: string) {
    if (!window.confirm(`Удалить цепочку «${name}»?`)) return;
    const next = { ...chains };
    delete next[name];
    await persist({ transitions: table, chains: next }, `Цепочка удалена: ${name}`);
  }

  // ─── Разметка ─────────────────────────────────────────────

  const registered = new Set(Object.values(CLIPS).map((c) => c.url));
  const loose = files
    .map((f) => 'anim/' + f.rel.split('/').map(encodeURIComponent).join('/'))
    .filter((p) => !registered.has(p));
  // Свои — из студии; встроенное, перекрытое пресетом, тоже считается своим.
  const mine = stateNames.filter((n) => n in PRESETS || !(n in BUILTIN_STATES));
  const builtin = stateNames.filter((n) => !mine.includes(n));
  const tableKeys = Object.keys(table).sort((a, b) => a.localeCompare(b, 'ru'));
  const chainNameOk = isChainName(chainName.trim());

  return (
    <>
      <section>
        <h2>Добавить шаг</h2>
        <div className="sb__sayrow">
          <select className="sb__select sb__select--grow" value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">— состояние или клип —</option>
            {mine.length > 0 && (
              <optgroup label="Мои состояния">
                {mine.map((n) => <option key={n} value={`state:${n}`}>{n}</option>)}
              </optgroup>
            )}
            <optgroup label="Встроенные состояния">
              {builtin.map((n) => <option key={n} value={`state:${n}`}>{n}</option>)}
            </optgroup>
            <optgroup label="Клипы">
              {Object.keys(CLIPS).map((n) => <option key={n} value={`clip:${n}`}>{n}</option>)}
            </optgroup>
            {loose.length > 0 && (
              <optgroup label="Файлы вне реестра">
                {loose.map((p) => <option key={p} value={`clip:${p}`}>{label(p)}</option>)}
              </optgroup>
            )}
          </select>
          <button className="sb__btn sb__btn--add" disabled={!pick} onClick={add}>+</button>
        </div>
      </section>

      <section>
        <div className="sb__row">
          <h2>Цепочка — {steps.length}</h2>
          {steps.length > 0 && !playing && (
            <button className="sb__link" onClick={() => setSteps([])}>очистить</button>
          )}
        </div>
        {steps.map((s, i) => {
          const prev = prevOf(steps, i, loop);
          const fade = fadeOf(s, prev);
          const sourceText = fade.source === 'шаг' ? '' : ` · ${fade.source === 'таблица' ? 'из таблицы' : fade.source === 'состояние' ? 'как у состояния' : 'обычный'}`;
          return (
            <div key={s.id} className={i === active ? 'sb__step sb__step--on' : 'sb__step'}>
              <div className="sb__step-head">
                <span className="sb__step-num">{i + 1}</span>
                <span className="sb__step-name" title={s.name}>
                  {label(s.name)}
                  <span className="sb__step-kind">{s.kind === 'state' ? 'состояние' : 'клип'}</span>
                </span>
                <button className="sb__icon" onClick={() => move(i, -1)} disabled={i === 0} title="выше">↑</button>
                <button className="sb__icon" onClick={() => move(i, 1)} disabled={i === steps.length - 1} title="ниже">↓</button>
                <button className="sb__icon" onClick={() => setSteps((l) => l.filter((x) => x.id !== s.id))} title="убрать">✕</button>
              </div>

              <div className="sb__row sb__row--gap">
                <button
                  className={s.mode === 'end' ? 'sb__chip sb__chip--on' : 'sb__chip'}
                  onClick={() => update(s.id, { mode: 'end' })}
                >
                  до конца клипа
                </button>
                <button
                  className={s.mode === 'secs' ? 'sb__chip sb__chip--on' : 'sb__chip'}
                  onClick={() => update(s.id, { mode: 'secs' })}
                >
                  секунд
                </button>
                {s.mode === 'secs' && (
                  <input
                    className="sb__num" type="number" min={0.5} max={60} step={0.5}
                    value={s.secs}
                    onChange={(e) => update(s.id, { secs: Math.max(0.5, Number(e.target.value) || 0.5) })}
                  />
                )}
              </div>

              <div className="sb__row sb__row--gap">
                <span className="sb__knob">
                  переход{prev ? ` из ${label(prev.name)}` : ' в шаг'}
                </span>
                <span className={fade.source === 'шаг' ? 'sb__val' : 'sb__val sb__val--muted'}>
                  {(fade.ms / 1000).toFixed(2)} с{sourceText}
                </span>
              </div>
              <input
                className="sb__range" type="range" min={0} max={1500} step={20}
                value={fade.ms}
                onChange={(e) => update(s.id, { fadeMs: Number(e.target.value) })}
                onDoubleClick={() => update(s.id, { fadeMs: null })}
                title="двойной клик — вернуть значение из таблицы или по умолчанию"
              />
            </div>
          );
        })}
        {!steps.length && (
          <p className="sb__hint">
            Добавьте два и больше шага — например, welcome → thinking → success —
            и смотрите, как Юми переходит из одного в другое.
          </p>
        )}
      </section>

      <section>
        <label className="sb__check">
          <input type="checkbox" checked={overlap} onChange={(e) => setOverlap(e.target.checked)} />
          перехлёст: следующий шаг вливается до конца текущего
        </label>
        <label className="sb__check">
          <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} />
          по кругу
        </label>
        <div className="sb__sayrow">
          <button
            className="sb__btn sb__btn--wide sb__btn--save"
            disabled={steps.length === 0}
            onClick={() => void play()}
          >
            {playing ? '↻ сначала' : '▶ проиграть'}
          </button>
          {playing && <button className="sb__btn sb__btn--wide" onClick={stop}>■ стоп</button>}
        </div>
        <button
          className="sb__btn sb__btn--wide"
          disabled={steps.length < 2}
          onClick={() => void saveTransitions()}
          title="подкрученные переходы — в таблицу; ядро будет применять их и вне цепочки"
        >
          ⇩ сохранить переходы в таблицу
        </button>
        {status && (
          <p className={status.ok ? 'sb__hint sb__hint--ok' : 'sb__hint sb__hint--warn'}>{status.text}</p>
        )}
      </section>

      <section>
        <h2>Сохранить цепочку</h2>
        <div className="sb__sayrow">
          <input
            className="sb__input sb__input--grow"
            placeholder="например: знакомство"
            value={chainName}
            maxLength={40}
            onChange={(e) => setChainName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void saveChain(); }}
          />
          <button
            className="sb__btn"
            disabled={!chainNameOk || !steps.length}
            onClick={() => void saveChain()}
          >
            {chainName.trim() in chains ? 'обновить' : 'сохранить'}
          </button>
        </div>
        {Object.entries(chains).map(([name, list]) => (
          <div key={name} className="sb__preset">
            <button
              className="sb__preset-main"
              onClick={() => { setSteps(withIds(list)); setChainName(name); setStatus(null); }}
              title="загрузить в редактор"
            >
              <span className="sb__file-name">{name}</span>
              <span className="sb__file-meta">{list.map((s) => label(s.name)).join(' → ')}</span>
            </button>
            <button className="sb__icon" onClick={() => void deleteChain(name)} title="удалить">✕</button>
          </div>
        ))}
      </section>

      <section>
        <h2>Таблица переходов — {tableKeys.length}</h2>
        {tableKeys.map((key) => {
          const [from, to] = splitTransitionKey(key);
          return (
            <div key={key} className="sb__trow">
              <span className="sb__trow-pair" title={key}>
                {label(from)} <span className="sb__trow-arrow">→</span> {label(to)}
              </span>
              <span className="sb__val">{(table[key] / 1000).toFixed(2)} с</span>
              <button className="sb__icon" onClick={() => void deleteTransition(key)} title="удалить">✕</button>
            </div>
          );
        })}
        <p className="sb__hint">
          {tableKeys.length
            ? 'Ядро применяет эти значения при смене состояний и в приложении: setState сам найдёт пару. В новых цепочках они подставляются автоматически.'
            : 'Пусто. Подкрутите «переход» у шагов цепочки и нажмите «сохранить переходы в таблицу».'}
        </p>
      </section>
    </>
  );
}
