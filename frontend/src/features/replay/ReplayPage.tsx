import { useEffect, useLayoutEffect, useReducer, useRef, useState, useSyncExternalStore } from 'react';
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

  useEffect(() => { loader.start(); return () => loader.stop(); }, [loader]);

  const duration = state.tape.durationMs;
  const now = performance.now();
  const pos = clock.position(now, duration);

  // Автопуск при первых данных — кроме prefers-reduced-motion.
  useEffect(() => {
    if (autoplayed.current || state.tape.segments.length === 0) return;
    autoplayed.current = true;
    if (!reducedMotion()) { clock.play(performance.now(), duration); redraw(); }
  }, [state, clock, duration]);

  // Пока идёт воспроизведение — перерисовка по кадрам.
  useEffect(() => {
    if (!clock.playing) return;
    let frame = requestAnimationFrame(function step() {
      clock.tick(performance.now(), loader.snapshot.tape.durationMs);
      redraw();
      if (clock.playing) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  });

  const visible = runs(state.tape.segments.filter((seg) => seg.offsetMs <= pos));

  // Новые строки — прокрутка вниз, если оператор не листает выше.
  useLayoutEffect(() => {
    const el = log.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [visible.length]);

  if (state.notFound) return <Empty>Запись сессии не найдена.</Empty>;
  if (state.startedAt === null && state.error) return <ErrorState message="Не удалось загрузить запись сессии." onRetry={() => loader.retry()} />;
  if (state.startedAt === null) return <Loading what="запись сессии" />;

  const act = (fn: (now: number) => void) => () => { fn(performance.now()); redraw(); };

  return (
    <Panel title="Запись сессии">
      <div className={s.meta}>
        <span>Начало: {formatTime(state.startedAt)}</span>
        {!state.complete && <StatusBadge tone="warn">запись продолжается</StatusBadge>}
      </div>
      {state.error !== null && <ErrorState message="Часть записи не загрузилась." onRetry={() => loader.retry()} />}
      <div className={s.controls}>
        <button className={ui.button} onClick={act((t) => (clock.playing ? clock.pause(t, duration) : clock.play(t, duration)))}>
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
      <div ref={log} className={s.screen} tabIndex={0} aria-label="Текст записи">
        {visible.length === 0 && <span className={s.hint}>Запись ещё не началась на этой позиции.</span>}
        {visible.map((run, i) => (run.gap
          ? <span key={i} className={s.gap}>[пропуск данных]</span>
          : <span key={i} className={s[run.direction]}><span className={s.srOnly}>{LABEL[run.direction]}: </span>{run.text}</span>))}
      </div>
    </Panel>
  );
}
