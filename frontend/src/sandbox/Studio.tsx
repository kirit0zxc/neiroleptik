import { useEffect, useMemo, useRef, useState } from 'react';
import type { MascotCore } from '../mascot/core/MascotCore';
import { CLIPS, DANCES, resolveClip } from '../mascot/core/clips';
import { DEFAULT_FADE_MS } from '../mascot/core/MascotCore';
import {
  BUILTIN_STATES, PRESETS, STATES, defineState, removeState,
} from '../mascot/core/states';
import { EXPRESSIONS, isPresetName, type Presets } from '../mascot/core/presets';
import type { FaceKey, StateSpec, VrmExpression } from '../mascot/core/types';
import { sortKeys } from '../mascot/core/faceTrack';
import type { VrmRenderer } from '../mascot/renderers/vrm/VrmRenderer';

type ClipState = NonNullable<ReturnType<VrmRenderer['getClipState']>>;

/** Служебное имя для предпросмотра — в списки состояний не попадает. */
export const PREVIEW_STATE = '__studio';

const EXPRESSION_LABELS: Record<VrmExpression, string> = {
  neutral: 'нейтральное',
  happy: 'радость',
  angry: 'злость',
  sad: 'грусть',
  relaxed: 'спокойствие',
  surprised: 'удивление',
};

const RANDOM_DANCE = '__dance';
const NO_CLIP = '';

type ResetMode = 'clip' | 'stay' | 'ms';

interface Draft {
  expression: VrmExpression;
  weight: number;
  clip: string;
  loop: boolean;
  reset: ResetMode;
  resetMs: number;
  speakOver: boolean;
  glowColor: string;
  glowIntensity: number;
  glowPulse: number;
  faceTrack: FaceKey[];
  /** Двигаться с места; null — как у клипа в реестре. */
  rootMotion: boolean | null;
  /** Плавность перехода в клип, мс. */
  fadeMs: number;
  /** Обрезка [начало, конец] в секундах клипа; null — целиком. */
  trim: [number, number] | null;
  /** Следить за курсором головой и глазами. */
  followPointer: boolean;
}

const EMPTY: Draft = {
  expression: 'happy', weight: 0.7,
  clip: 'victory', loop: false,
  reset: 'clip', resetMs: 3000, speakOver: true,
  glowColor: '#9ED8BC', glowIntensity: 0.5, glowPulse: 0,
  faceTrack: [],
  rootMotion: null,
  fadeMs: DEFAULT_FADE_MS,
  trim: null,
  followPointer: false,
};

function toSpec(d: Draft): StateSpec {
  const action = d.clip === RANDOM_DANCE ? DANCES : d.clip || undefined;
  // Без клипа или с повтором «конец клипа» не наступит — значит, остаёмся.
  const reset = d.reset === 'clip' && (!action || d.loop) ? 'stay' : d.reset;
  return {
    expression: d.expression,
    weight: d.expression === 'neutral' ? 0 : d.weight,
    ...(action ? { action } : {}),
    loop: d.loop,
    resetAfterMs: reset === 'clip' ? 'clip' : reset === 'stay' ? null : d.resetMs,
    ...(d.speakOver ? { speakOver: true } : {}),
    ...(action && d.faceTrack.length ? { faceTrack: sortKeys(d.faceTrack) } : {}),
    ...(action && d.rootMotion != null ? { rootMotion: d.rootMotion } : {}),
    ...(action && d.fadeMs !== DEFAULT_FADE_MS ? { fadeMs: d.fadeMs } : {}),
    ...(typeof action === 'string' && d.trim ? { trim: d.trim } : {}),
    ...(d.followPointer ? { followPointer: true } : {}),
    glow: { color: d.glowColor, intensity: d.glowIntensity, pulseHz: d.glowPulse },
  };
}

function fromSpec(s: StateSpec): Draft {
  const clip = Array.isArray(s.action)
    ? RANDOM_DANCE
    : typeof s.action === 'string' ? s.action : NO_CLIP;
  return {
    expression: s.expression,
    weight: s.expression === 'neutral' ? EMPTY.weight : s.weight,
    clip,
    loop: s.loop ?? false,
    reset: s.resetAfterMs === 'clip' ? 'clip' : s.resetAfterMs == null ? 'stay' : 'ms',
    resetMs: typeof s.resetAfterMs === 'number' ? s.resetAfterMs : EMPTY.resetMs,
    speakOver: s.speakOver ?? false,
    glowColor: /^#[0-9a-f]{6}$/i.test(s.glow.color) ? s.glow.color : EMPTY.glowColor,
    glowIntensity: s.glow.intensity,
    glowPulse: s.glow.pulseHz,
    faceTrack: s.faceTrack ? [...s.faceTrack] : [],
    rootMotion: s.rootMotion ?? null,
    fadeMs: s.fadeMs ?? DEFAULT_FADE_MS,
    trim: s.trim ? [s.trim[0], s.trim[1]] : null,
    followPointer: s.followPointer === true,
  };
}

/** Короткое описание связки для списка сохранённых. */
function summary(s: StateSpec) {
  const clip = Array.isArray(s.action) ? 'случайный танец'
    : typeof s.action === 'string' ? decodeURIComponent(s.action.split('/').pop()!) : 'без клипа';
  const face = s.expression === 'neutral'
    ? EXPRESSION_LABELS.neutral
    : `${EXPRESSION_LABELS[s.expression]} ${Math.round(s.weight * 100)}%`;
  const track = s.faceTrack?.length ? ` · меток: ${s.faceTrack.length}` : '';
  return `${face} · ${clip}${track}`;
}

/**
 * Поле секунд, которое применяется по Enter или при уходе из поля.
 * Каждая цифра по отдельности перезапускала бы клип с новой обрезкой.
 */
function TimeInput({ value, onCommit, title }: { value: number; onCommit: (v: number) => void; title: string }) {
  const [text, setText] = useState(value.toFixed(2));
  // Значение поменялось снаружи (кнопки «здесь») — показываем его.
  const [shown, setShown] = useState(value);
  if (shown !== value) { setShown(value); setText(value.toFixed(2)); }
  const commit = () => {
    const v = Number(text.replace(',', '.'));
    if (Number.isFinite(v)) onCommit(v);
    else setText(value.toFixed(2));
  };
  return (
    <input
      className="sb__num" inputMode="decimal" title={title}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
    />
  );
}

interface StudioProps {
  core: MascotCore;
  /** Список состояний изменился — песочнице надо перерисовать кнопки. */
  onStatesChange: () => void;
  /** Файлы из папки anim/ (манифест песочницы) — пути относительно anim/. */
  files?: { rel: string; file: string }[];
}

/**
 * Студия: собрать выражение лица, клип и свет в одно состояние
 * и сохранить под своим именем в presets.json.
 */
export function Studio({ core, onStatesChange, files = [] }: StudioProps) {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [name, setName] = useState('');
  const [presets, setPresets] = useState<Presets>(() => ({ ...PRESETS }));
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // Пока ничего не трогали, предпросмотр не перехватывает Юми.
  const touched = useRef(false);

  const spec = useMemo(() => toSpec(draft), [draft]);
  const specKey = JSON.stringify(spec);

  const lastMotion = useRef('');

  /**
   * Показать черновик на Юми.
   *
   * Пока редактируем, клип крутится по кругу — иначе таймлайн исчезал
   * бы с концом клипа. Если клип не менялся, лицо и свет обновляются
   * на ходу: правка метки не должна отматывать анимацию в начало.
   */
  const preview = () => {
    const single = typeof spec.action === 'string' ? spec.action : null;
    defineState(PREVIEW_STATE, single ? { ...spec, loop: true, resetAfterMs: null } : spec);
    // Смена маски — это другие дорожки клипа, тут без перезапуска не обойтись.
    const motion = JSON.stringify([spec.action ?? null, spec.rootMotion ?? null, spec.trim ?? null]);
    const r = core.getRenderer();
    if (r && motion === lastMotion.current && core.getState() === PREVIEW_STATE) {
      r.setExpression(spec.expression, spec.weight);
      r.setFaceTrack?.(single, spec.faceTrack ?? null);
      r.setGlow(spec.glow);
      return;
    }
    lastMotion.current = motion;
    core.setState(PREVIEW_STATE);
  };

  /** Проиграть так, как будет после сохранения: разово, с возвратом в покой. */
  const showAsSaved = () => {
    touched.current = true;
    lastMotion.current = '';
    defineState(PREVIEW_STATE, spec);
    core.setState(PREVIEW_STATE);
  };

  // Время клипа для таймлайна. Опрос, а не подписка: рендерер о каждом
  // кадре не сообщает, а 20 раз в секунду глазу хватает.
  const [clipState, setClipState] = useState<ClipState | null>(null);
  useEffect(() => {
    const id = setInterval(() => {
      const r = core.getRenderer() as VrmRenderer | null;
      const next = r?.getClipState?.() ?? null;
      setClipState((prev) => (prev && next && prev.name === next.name
        && Math.abs(prev.time - next.time) < 0.01 && prev.paused === next.paused ? prev : next));
    }, 50);
    return () => clearInterval(id);
  }, [core]);

  const renderer = () => core.getRenderer() as VrmRenderer | null;
  const singleClip = draft.clip && draft.clip !== RANDOM_DANCE ? draft.clip : null;
  const timeline = singleClip && clipState?.name === singleClip ? clipState : null;

  const setKeys = (keys: FaceKey[]) => edit({ faceTrack: sortKeys(keys) });

  /** Обрезка: начало раньше конца хотя бы на 0.1 с; вся длина — без обрезки. */
  const setTrim = (start: number, end: number, duration: number) => {
    const round = (v: number) => Math.round(v * 100) / 100;
    const s = round(Math.min(Math.max(start, 0), duration - 0.1));
    const e = round(Math.min(Math.max(end, s + 0.1), duration));
    edit({ trim: s <= 0.005 && e >= duration - 0.005 ? null : [s, e] });
  };
  const addKey = () => {
    if (!timeline) return;
    const at = Math.round(timeline.time * 100) / 100;
    // Новая метка начинается с того лица, что сейчас на экране.
    const current = draft.faceTrack.filter((k) => k.at <= at).at(-1)
      ?? { expression: draft.expression, weight: draft.weight };
    setKeys([
      ...draft.faceTrack.filter((k) => k.at !== at),
      { at, expression: current.expression, weight: current.weight || 0.7 },
    ]);
  };
  const updateKey = (i: number, patch: Partial<FaceKey>) =>
    setKeys(draft.faceTrack.map((k, j) => (j === i ? { ...k, ...patch } : k)));
  const removeKey = (i: number) => setKeys(draft.faceTrack.filter((_, j) => j !== i));
  const seek = (t: number) => {
    renderer()?.seekClip(t);
    renderer()?.setClipPaused(true);
  };

  useEffect(() => {
    if (touched.current) preview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specKey]);

  const edit = (patch: Partial<Draft>) => {
    touched.current = true;
    setDraft((d) => ({ ...d, ...patch }));
  };

  const trimmed = name.trim();
  const nameOk = isPresetName(trimmed);
  const overridesBuiltin = trimmed in BUILTIN_STATES;
  const exists = trimmed in presets;

  /** Записать набор в файл; при ошибке на экране остаётся прежний. */
  async function persist(next: Presets, message: string) {
    setBusy(true);
    try {
      const res = await fetch('/mascot/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setPresets(next);
      setStatus({ ok: true, text: message });
      return true;
    } catch {
      setStatus({
        ok: false,
        text: 'Не сохранилось: запись в файл работает только из npm run dev. '
          + 'JSON можно скопировать кнопкой ниже.',
      });
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!nameOk) return;
    const ok = await persist({ ...presets, [trimmed]: spec }, `Сохранено: ${trimmed}`);
    if (!ok) return;
    defineState(trimmed, spec);
    onStatesChange();
  }

  async function remove(key: string) {
    if (!window.confirm(`Удалить «${key}» из presets.json?`)) return;
    const next = { ...presets };
    delete next[key];
    if (!(await persist(next, `Удалено: ${key}`))) return;
    removeState(key);
    onStatesChange();
  }

  function load(key: string) {
    const s = presets[key] ?? STATES[key];
    if (!s) return;
    touched.current = true;
    setDraft(fromSpec(s));
    setName(key in presets || key in BUILTIN_STATES ? key : '');
    setStatus(null);
  }

  const clipNames = Object.keys(CLIPS).filter((c) => !DANCES.includes(c));
  // Файлы, которых нет в реестре: их можно поставить в состояние по пути.
  const registered = new Set(Object.values(CLIPS).map((c) => c.url));
  const loose = files
    .map((f) => ({ path: 'anim/' + f.rel.split('/').map(encodeURIComponent).join('/'), label: f.file }))
    .filter((f) => !registered.has(f.path));

  return (
    <>

      <section>
        <h2>Взять за основу</h2>
        <select
          className="sb__select"
          value=""
          onChange={(e) => { if (e.target.value) load(e.target.value); }}
        >
          <option value="">— выбрать состояние —</option>
          {Object.keys(presets).length > 0 && (
            <optgroup label="Мои">
              {Object.keys(presets).map((k) => <option key={k} value={k}>{k}</option>)}
            </optgroup>
          )}
          <optgroup label="Встроенные">
            {Object.keys(BUILTIN_STATES).map((k) => <option key={k} value={k}>{k}</option>)}
          </optgroup>
        </select>
      </section>

      <section>
        <h2>{draft.faceTrack.length && singleClip ? 'Выражение до первой метки и после клипа' : 'Выражение лица'}</h2>
        <div className="sb__grid">
          {EXPRESSIONS.map((ex) => (
            <button
              key={ex}
              className={ex === draft.expression ? 'sb__btn sb__btn--active' : 'sb__btn'}
              onClick={() => edit({ expression: ex })}
            >
              {EXPRESSION_LABELS[ex]}
            </button>
          ))}
        </div>
        {draft.expression !== 'neutral' && (
          <>
            <div className="sb__row sb__row--gap">
              <span className="sb__knob">сила</span>
              <span className="sb__val">{Math.round(draft.weight * 100)}%</span>
            </div>
            <input
              className="sb__range" type="range" min={0.05} max={1} step={0.05}
              value={draft.weight}
              onChange={(e) => edit({ weight: Number(e.target.value) })}
            />
          </>
        )}
      </section>

      <section>
        <h2>Анимация</h2>
        <select
          className="sb__select"
          value={draft.clip}
          onChange={(e) => edit({
            clip: e.target.value,
            rootMotion: null,
            trim: null,
            ...(!e.target.value && draft.reset === 'clip' ? { reset: 'stay' } : {}),
          })}
        >
          <option value={NO_CLIP}>без анимации — только лицо и свет</option>
          <optgroup label="Клипы">
            {clipNames.map((c) => <option key={c} value={c}>{c}</option>)}
          </optgroup>
          <optgroup label="Танцы">
            <option value={RANDOM_DANCE}>случайный танец</option>
            {DANCES.map((c) => <option key={c} value={c}>{c}</option>)}
          </optgroup>
          {loose.length > 0 && (
            <optgroup label="Файлы вне реестра">
              {loose.map((f) => <option key={f.path} value={f.path}>{f.label}</option>)}
            </optgroup>
          )}
        </select>

        <label className="sb__check">
          <input
            type="checkbox" checked={draft.loop}
            onChange={(e) => edit({ loop: e.target.checked, ...(e.target.checked && draft.reset === 'clip' ? { reset: 'stay' } : {}) })}
          />
          повторять по кругу
        </label>
        <label className="sb__check">
          <input
            type="checkbox" checked={draft.speakOver}
            onChange={(e) => edit({ speakOver: e.target.checked })}
          />
          говорить поверх, не прерывая анимацию
        </label>
        <label className="sb__check">
          <input
            type="checkbox" checked={draft.followPointer}
            onChange={(e) => edit({ followPointer: e.target.checked })}
          />
          следить за курсором (иначе — смотрит в камеру)
        </label>
        {draft.clip && (
          <label className="sb__check">
            <input
              type="checkbox"
              checked={draft.rootMotion ?? (draft.clip !== RANDOM_DANCE && resolveClip(draft.clip)?.mask === 'none')}
              onChange={(e) => edit({ rootMotion: e.target.checked })}
            />
            двигаться с места, как в клипе — приседания, прыжки, шаги; камера стоит
          </label>
        )}
        {draft.clip && (
          <>
            <div className="sb__row sb__row--gap">
              <span className="sb__knob">плавность перехода</span>
              <span className="sb__val">{(draft.fadeMs / 1000).toFixed(2)} с</span>
            </div>
            <input
              className="sb__range" type="range" min={0} max={1500} step={20}
              value={draft.fadeMs}
              onChange={(e) => edit({ fadeMs: Number(e.target.value) })}
              onDoubleClick={() => edit({ fadeMs: DEFAULT_FADE_MS })}
              title="за сколько клип вливается в предыдущий; двойной клик — 0.28 с"
            />
          </>
        )}
      </section>

      <section>
        <h2>Когда вернуться в покой</h2>
        <div className="sb__masks">
          {([
            ['clip', 'по концу клипа'],
            ['ms', 'через время'],
            ['stay', 'не возвращаться'],
          ] as [ResetMode, string][]).map(([mode, label]) => (
            <button
              key={mode}
              className={draft.reset === mode ? 'sb__chip sb__chip--on' : 'sb__chip'}
              disabled={mode === 'clip' && (draft.loop || !draft.clip)}
              onClick={() => edit({ reset: mode })}
            >
              {label}
            </button>
          ))}
        </div>
        {draft.reset === 'ms' && (
          <>
            <div className="sb__row">
              <span className="sb__knob">через</span>
              <span className="sb__val">{(draft.resetMs / 1000).toFixed(1)} с</span>
            </div>
            <input
              className="sb__range" type="range" min={500} max={30000} step={500}
              value={draft.resetMs}
              onChange={(e) => edit({ resetMs: Number(e.target.value) })}
            />
          </>
        )}
      </section>

      <section>
        <h2>Свечение контура</h2>
        <div className="sb__row sb__row--gap">
          <input
            className="sb__color" type="color" value={draft.glowColor}
            onChange={(e) => edit({ glowColor: e.target.value })}
          />
          <span className="sb__knob">сила</span>
          <span className="sb__val">{Math.round(draft.glowIntensity * 100)}%</span>
        </div>
        <input
          className="sb__range" type="range" min={0} max={1} step={0.05}
          value={draft.glowIntensity}
          onChange={(e) => edit({ glowIntensity: Number(e.target.value) })}
        />
        <div className="sb__row">
          <span className="sb__knob">пульсация</span>
          <span className="sb__val">{draft.glowPulse ? `${draft.glowPulse.toFixed(1)} Гц` : 'нет'}</span>
        </div>
        <input
          className="sb__range" type="range" min={0} max={3} step={0.1}
          value={draft.glowPulse}
          onChange={(e) => edit({ glowPulse: Number(e.target.value) })}
        />
      </section>

      <section>
        <h2>Таймлайн: обрезка и лицо</h2>
        {!singleClip && (
          <p className="sb__hint">
            Выберите один клип — у случайного танца и «без анимации» дорожки нет.
          </p>
        )}
        {singleClip && !timeline && (
          <p className="sb__hint">Клип грузится или ещё не запущен — измените что-нибудь выше.</p>
        )}
        {timeline && (
          <>
            <div
              className="sb__timeline"
              title="нажмите, чтобы перейти"
              onClick={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                seek(((e.clientX - rect.left) / rect.width) * timeline.duration);
              }}
            >
              {/* Затенено то, что обрезано. */}
              <span className="sb__cut" style={{ left: 0, width: `${(timeline.start / timeline.duration) * 100}%` }} />
              <span
                className="sb__cut"
                style={{ left: `${(timeline.end / timeline.duration) * 100}%`, right: 0 }}
              />
              {draft.faceTrack.map((k) => (
                <span
                  key={k.at}
                  className="sb__mark"
                  style={{ left: `${(k.at / timeline.duration) * 100}%` }}
                  title={`${k.at.toFixed(2)} с — ${EXPRESSION_LABELS[k.expression]}`}
                />
              ))}
              <span
                className="sb__playhead"
                style={{ left: `${(timeline.time / timeline.duration) * 100}%` }}
              />
            </div>
            <div className="sb__row sb__row--gap">
              <button
                className="sb__icon sb__icon--play"
                onClick={() => renderer()?.setClipPaused(!timeline.paused)}
                title={timeline.paused ? 'продолжить' : 'пауза'}
              >
                {timeline.paused ? '▶' : '❚❚'}
              </button>
              <input
                className="sb__range sb__range--flex" type="range"
                min={0} max={timeline.duration} step={0.01}
                value={timeline.time}
                onChange={(e) => seek(Number(e.target.value))}
              />
              <span className="sb__val">
                {timeline.time.toFixed(2)} / {timeline.duration.toFixed(1)} с
              </span>
            </div>
            <div className="sb__trim">
              <span className="sb__knob">обрезка</span>
              <TimeInput
                value={draft.trim?.[0] ?? 0}
                onCommit={(v) => setTrim(v, draft.trim?.[1] ?? timeline.duration, timeline.duration)}
                title="начало, с"
              />
              <span className="sb__knob">—</span>
              <TimeInput
                value={draft.trim?.[1] ?? timeline.duration}
                onCommit={(v) => setTrim(draft.trim?.[0] ?? 0, v, timeline.duration)}
                title="конец, с"
              />
              {draft.trim && (
                <button className="sb__icon" onClick={() => edit({ trim: null })} title="без обрезки">✕</button>
              )}
            </div>
            <div className="sb__sayrow">
              <button
                className="sb__btn sb__btn--wide"
                onClick={() => setTrim(timeline.time, draft.trim?.[1] ?? timeline.duration, timeline.duration)}
                title="начинать клип с текущего момента"
              >
                ⇤ начало здесь
              </button>
              <button
                className="sb__btn sb__btn--wide"
                onClick={() => setTrim(draft.trim?.[0] ?? 0, timeline.time, timeline.duration)}
                title="заканчивать клип на текущем моменте"
              >
                конец здесь ⇥
              </button>
            </div>
            <button className="sb__btn sb__btn--wide" onClick={addKey}>
              + выражение на {timeline.time.toFixed(2)} с
            </button>
          </>
        )}
        {singleClip && draft.faceTrack.map((k, i) => (
          <div key={`${k.at}-${i}`} className="sb__key">
            <input
              className="sb__num" type="number" min={0} step={0.1}
              max={timeline?.duration}
              value={k.at}
              onChange={(e) => updateKey(i, { at: Math.max(0, Number(e.target.value) || 0) })}
              title="секунда клипа"
            />
            <select
              className="sb__select sb__select--inline"
              value={k.expression}
              onChange={(e) => updateKey(i, { expression: e.target.value as VrmExpression })}
            >
              {EXPRESSIONS.map((ex) => <option key={ex} value={ex}>{EXPRESSION_LABELS[ex]}</option>)}
            </select>
            <input
              className="sb__range sb__range--flex" type="range" min={0.05} max={1} step={0.05}
              disabled={k.expression === 'neutral'}
              value={k.weight}
              onChange={(e) => updateKey(i, { weight: Number(e.target.value) })}
              title={`сила ${Math.round(k.weight * 100)}%`}
            />
            <button className="sb__icon" onClick={() => seek(k.at)} title="перейти">⤳</button>
            <button className="sb__icon" onClick={() => removeKey(i)} title="убрать">✕</button>
          </div>
        ))}
        {singleClip && draft.faceTrack.length > 0 && (
          <p className="sb__hint">Метка действует до следующей; между ними лицо меняется плавно.</p>
        )}
      </section>

      <section>
        <button className="sb__btn sb__btn--wide" onClick={showAsSaved}>
          ▶ показать как будет
        </button>
        <p className="sb__hint">
          Пока вы редактируете, клип крутится по кругу. Эта кнопка проигрывает
          его так, как после сохранения.
        </p>
      </section>

      <section>
        <h2>Сохранить как</h2>
        <input
          className="sb__input"
          placeholder="например: celebrate"
          value={name}
          maxLength={40}
          onChange={(e) => { setName(e.target.value); setStatus(null); }}
          onKeyDown={(e) => { if (e.key === 'Enter') void save(); }}
        />
        {trimmed && !nameOk && (
          <p className="sb__hint sb__hint--warn">Только буквы, цифры, _ и -, до 40 символов.</p>
        )}
        {nameOk && overridesBuiltin && (
          <p className="sb__hint sb__hint--warn">
            Перекроет встроенное состояние «{trimmed}» — его будут вызывать и правила моста.
          </p>
        )}
        <button
          className="sb__btn sb__btn--wide sb__btn--save"
          disabled={!nameOk || busy}
          onClick={() => void save()}
        >
          {exists ? `обновить «${trimmed}»` : 'сохранить'}
        </button>
        {status && (
          <p className={status.ok ? 'sb__hint sb__hint--ok' : 'sb__hint sb__hint--warn'}>{status.text}</p>
        )}
      </section>

      <section>
        <div className="sb__row">
          <h2>Мои состояния — {Object.keys(presets).length}</h2>
          {Object.keys(presets).length > 0 && (
            <button
              className="sb__link"
              onClick={() => void navigator.clipboard.writeText(JSON.stringify(presets, null, 2))}
            >
              копировать JSON
            </button>
          )}
        </div>
        {Object.entries(presets).map(([key, s]) => (
          <div key={key} className="sb__preset">
            <button className="sb__preset-main" onClick={() => core.setState(key)} title="показать">
              <span className="sb__file-name">▶ {key}</span>
              <span className="sb__file-meta">{summary(s)}</span>
            </button>
            <button className="sb__icon" onClick={() => load(key)} title="редактировать">✎</button>
            <button className="sb__icon" onClick={() => void remove(key)} title="удалить">✕</button>
          </div>
        ))}
        {!Object.keys(presets).length && (
          <p className="sb__hint">
            Пока пусто. Сохранённое вызывается как обычное состояние:
            <code> getMascot().setState('имя')</code>
          </p>
        )}
      </section>
    </>
  );
}
