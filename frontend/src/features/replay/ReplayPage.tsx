import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState, useSyncExternalStore } from 'react';
import { useParams } from 'react-router';
import { formatTime } from '../../shared/format/time';
import { Empty, ErrorState, Loading, Panel, StatusBadge, ui } from '../../shared/ui';
import { formatOffset, ReplayClock, SPEEDS, type Speed } from './clock';
import type { Direction, Segment } from './tape';
import { fetchReplay, ReplayLoader, type FetchReplay } from './loader';
import s from './replay.module.css';

const LABEL = { in: 'Ввод атакующего', out: 'Вывод ловушки' } as const;

/** Подряд идущие части одного направления — один блок (одна подпись для читалок). */
function runs(segments: readonly Segment[]): { direction: Direction; text: string; gap: boolean }[] {
  const out: { direction: Direction; text: string; gap: boolean }[] = [];
  for (const seg of segments) {
    const prev = out.at(-1);
    if ('gap' in seg) out.push({ direction: seg.direction, text: '', gap: true });
    else if (prev && !prev.gap && prev.direction === seg.direction) prev.text += seg.text;
    else out.push({ direction: seg.direction, text: seg.text, gap: false });
  }
  return out;
}

function reducedMotion() {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** Экран сессии (F15): пока только проигрыватель записи. */
export function SessionPage({ fetch = fetchReplay }: { fetch?: FetchReplay }) {
  const { id = '' } = useParams();
  return (
    <>
      <h1>Сессия <span className="mono">{id}</span></h1>
      <Empty>Паспорт сессии появится после согласования формата.</Empty>
      {/* key: другая сессия — новый загрузчик и часы. */}
      <ReplayPlayer key={id} id={id} fetch={fetch} />
    </>
  );
}

function ReplayPlayer({ id, fetch }: { id: string; fetch: FetchReplay }) {
  const [loader] = useState(() => new ReplayLoader(id, fetch));
  const state = useSyncExternalStore(loader.subscribe, () => loader.snapshot);
  const clock = useRef(new ReplayClock()).current;
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  const autoplayed = useRef(false);
  const log = useRef<HTMLDivElement>(null);
  /** Оператор у низа ленты — следуем за записью. Считается по прокрутке, до роста текста. */
  const stick = useRef(true);

  useEffect(() => { loader.start(); return () => loader.stop(); }, [loader]);

  const duration = state.tape.durationMs;
  const now = performance.now();
  const pos = clock.position(now, duration);
  // Воспроизведение идёт — для зависимостей эффекта анимации (пуск/пауза его
  // перезапускают, а перерисовки по кадрам — нет).
  const playing = clock.playing;

  // Автопуск при первых данных — кроме prefers-reduced-motion.
  useEffect(() => {
    if (autoplayed.current || state.tape.segments.length === 0) return;
    autoplayed.current = true;
    if (!reducedMotion()) { clock.play(performance.now(), duration, state.complete); redraw(); }
  }, [state, clock, duration]);

  // Пока идёт воспроизведение — перерисовка по кадрам. У живого края, когда
  // позиция упёрлась в конец и новых данных ещё нет, кадры не крутим впустую
  // (иначе постоянная CPU-нагрузка): цикл перезапустится по приходе данных,
  // так как эффект зависит от state.version.
  useEffect(() => {
    if (!clock.playing) return;
    let frame = requestAnimationFrame(function step() {
      const snap = loader.snapshot;
      clock.tick(performance.now(), snap.tape.durationMs, snap.complete);
      redraw();
      const caughtUpLive = !snap.complete && clock.position(performance.now(), snap.tape.durationMs) >= snap.tape.durationMs;
      if (clock.playing && !caughtUpLive) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
    // playing — перезапуск по пуску/паузе; pos — по перемотке (seek меняет только
    // часы, иначе у живого края кадры не возобновились бы); state.version — по
    // приходе новых данных живого хвоста. В простое у края перерисовок нет, поэтому
    // холостой нагрузки не возникает.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clock, playing, pos, state.version]);

  // Число видимых частей меняется редко; склейку блоков пересчитываем только тогда,
  // а не на каждом кадре воспроизведения.
  const count = state.tape.segments.reduce((n, seg) => (seg.offsetMs <= pos ? n + 1 : n), 0);
  const visible = useMemo(
    () => runs(state.tape.segments.filter((seg) => seg.offsetMs <= pos)),
    // pos намеренно не в зависимостях: набор частей определяется count и версией ленты.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.version, count],
  );

  // Новые строки — прокрутка вниз, если оператор не листает выше. Положение
  // проверяем до добавления текста (в onScroll): большой блок вывода иначе сам
  // «отодвигает» низ, и следование обрывалось бы.
  useLayoutEffect(() => {
    const el = log.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [count, state.version]);

  if (state.notFound) return <Empty>Запись сессии не найдена.</Empty>;
  if (state.startedAt === null && state.error) return <ErrorState message="Не удалось загрузить запись сессии." onRetry={() => loader.retry()} />;
  if (state.startedAt === null) return <Loading what="запись сессии" />;

  const act = (fn: (now: number) => void) => () => { fn(performance.now()); redraw(); };

  return (
    <Panel title="Запись сессии">
      <div className={s.meta}>
        <span>Начало: {formatTime(state.startedAt)}</span>
        {!state.complete && <StatusBadge tone="warn">запись продолжается</StatusBadge>}
        {/* Только для загруженной целиком записи: пока она идёт, вывод может прийти позже. */}
        {state.complete && state.tape.isEmpty && <StatusBadge tone="idle">Запись пуста</StatusBadge>}
        {state.complete && state.tape.inputWithoutOutput && (
          <StatusBadge tone="warn">Ответы ловушки в записи отсутствуют</StatusBadge>
        )}
      </div>
      {state.error !== null && <ErrorState message="Часть записи не загрузилась." onRetry={() => loader.retry()} />}
      <div className={s.controls}>
        <button className={ui.button} onClick={act((t) => (clock.playing ? clock.pause(t, duration) : clock.play(t, duration, state.complete)))}>
          {clock.playing ? 'Пауза' : 'Пуск'}
        </button>
        <button className={ui.button} onClick={act((t) => clock.seek(0, t, duration))}>В начало</button>
        <button className={ui.button} onClick={act((t) => clock.seek(duration, t, duration))}>В конец</button>
        <label className={s.speed}>
          Скорость
          <select value={clock.speed} onChange={(e) => act((t) => clock.setSpeed(Number(e.target.value) as Speed, t, duration))()}>
            {SPEEDS.map((v) => <option key={v} value={v}>×{String(v).replace('.', ',')}</option>)}
          </select>
        </label>
        <input
          className={s.range} type="range" min={0} max={duration} step={100} value={pos}
          aria-label="Позиция записи" aria-valuetext={`${formatOffset(pos)} из ${formatOffset(duration)}`}
          onChange={(e) => act((t) => clock.seek(Number(e.target.value), t, duration))()}
        />
        <span className={s.time}>{formatOffset(pos)} / {formatOffset(duration)}</span>
      </div>
      <div className={s.legend}>
        <span className={s.in}>■ {LABEL.in}</span> <span className={s.out}>■ {LABEL.out}</span>
      </div>
      <div ref={log} onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }} className={s.screen} tabIndex={0} role="region" aria-label="Текст записи">
        {visible.length === 0 && <span className={s.hint}>Запись ещё не началась на этой позиции.</span>}
        {visible.map((run, i) => (run.gap
          ? <span key={i} className={s.gap}>[пропуск данных]</span>
          : <span key={i} className={s[run.direction]}><span className={s.srOnly}>{LABEL[run.direction]}: </span>{run.text}</span>))}
      </div>
    </Panel>
  );
}
