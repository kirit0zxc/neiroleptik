import { useEffect, useRef, useState } from 'react';
import type { MascotCore } from '../mascot/core/MascotCore';
import type { VrmRenderer } from '../mascot/renderers/vrm/VrmRenderer';
import {
  CAMERA_DEFAULTS, CAMERA_LIMITS, cameraDiff, parseCamera, type CameraSettings,
} from '../mascot/renderers/vrm/camera';
import { parseClipCameras, type ClipCameras } from '../mascot/renderers/vrm/clipCameras';
import cameraFile from '../mascot/renderers/vrm/camera.json';
import clipCamerasFile from '../mascot/renderers/vrm/clipCameras.json';

type NumKey = keyof typeof CAMERA_LIMITS;

const SLIDERS: { key: NumKey; label: string; step: number; unit: (v: number) => string }[] = [
  { key: 'zoom', label: 'приближение', step: 0.01, unit: (v) => `×${v.toFixed(2)}` },
  { key: 'offsetY', label: 'Юми выше / ниже', step: 0.01, unit: (v) => `${v.toFixed(2)} м` },
  { key: 'offsetX', label: 'Юми правее / левее', step: 0.01, unit: (v) => `${v.toFixed(2)} м` },
  { key: 'yaw', label: 'поворот вокруг', step: 1, unit: (v) => `${Math.round(v)}°` },
  { key: 'pitch', label: 'наклон камеры', step: 1, unit: (v) => `${Math.round(v)}°` },
  { key: 'fov', label: 'угол обзора', step: 1, unit: (v) => `${Math.round(v)}°` },
  { key: 'margin', label: 'поля по краям', step: 0.01, unit: (v) => `${Math.round(v * 100)}%` },
];

const FITS: [CameraSettings['fit'], string][] = [
  ['full', 'во весь рост'],
  ['half', 'по пояс'],
  ['portrait', 'портрет'],
];

/** Сохранённый в файле общий вид — к нему возвращает «к сохранённому». */
const SAVED: CameraSettings = { ...CAMERA_DEFAULTS, ...parseCamera(cameraFile) };

const clamp = (key: NumKey, v: number) => {
  const [lo, hi] = CAMERA_LIMITS[key];
  return Math.min(hi, Math.max(lo, v));
};

/** Имя клипа для людей: путь к файлу — только имя файла. */
const clipLabel = (clip: string) => decodeURIComponent(clip.split('/').pop() ?? clip);

/** Настройки кадра строкой для кода: только то, что отличается от встроенного. */
function codeFor(cam: CameraSettings) {
  const diff = cameraDiff(cam);
  const parts = Object.entries(diff).map(([k, v]) => `${k}: ${typeof v === 'string' ? `'${v}'` : v}`);
  return parts.length ? `renderer={{ ${parts.join(', ')} }}` : 'renderer={{}}  // всё по умолчанию';
}

type Scope = 'all' | 'clip';

interface CameraPanelProps {
  core: MascotCore;
  ready: boolean;
  /** Сцена, на которой работают мышь и колесо. */
  stage: HTMLElement | null;
}

/**
 * Камера: кадр, крупность, сдвиг, поворот и наклон вокруг Юми.
 * Две области: общий кадр для всех анимаций и свой кадр для той,
 * что играет сейчас. Мышью на сцене: колесо — приближение,
 * перетаскивание — поворот, Shift + перетаскивание — сдвиг.
 */
export function CameraPanel({ core, ready, stage }: CameraPanelProps) {
  const [scope, setScope] = useState<Scope>('all');
  const [clip, setClip] = useState<string | null>(null);
  const [cam, setCam] = useState<CameraSettings>(SAVED);
  const [saved, setSaved] = useState<CameraSettings>(SAVED);
  const [savedClips, setSavedClips] = useState<ClipCameras>(() => parseClipCameras(clipCamerasFile));
  const [liveClips, setLiveClips] = useState<ClipCameras>(() => parseClipCameras(clipCamerasFile));
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const camRef = useRef(cam);
  useEffect(() => { camRef.current = cam; }, [cam]);
  const ctx = useRef({ scope, clip });
  useEffect(() => { ctx.current = { scope, clip }; }, [scope, clip]);

  const renderer = () => core.getRenderer() as VrmRenderer | null;

  // Какая анимация играет — для режима «только для текущей».
  useEffect(() => {
    const id = setInterval(() => {
      const name = renderer()?.getClipState?.()?.name ?? null;
      setClip((prev) => (prev === name ? prev : name));
    }, 300);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [core]);

  // Сменилась область или клип — показываем то, что для них действует.
  useEffect(() => {
    const r = renderer();
    if (!ready || !r?.getCamera) return;
    if (scope === 'all') setCam(r.getCamera());
    else if (clip) setCam({ ...r.getCamera(), ...(r.getClipCamera(clip) ?? {}) });
    setStatus(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, scope, clip]);

  const apply = (patch: Partial<CameraSettings>) => {
    const next = { ...camRef.current, ...parseCamera(patch) };
    camRef.current = next;
    setCam(next);
    setStatus(null);
    const r = renderer();
    if (!r) return;
    const { scope: s, clip: c } = ctx.current;
    if (s === 'all') {
      r.setCamera(next);
    } else if (c) {
      // У клипа храним только отличия от общего кадра.
      r.setClipCamera(c, cameraDiff(next, r.getCamera()));
      setLiveClips(r.getClipCameras());
    }
  };

  // Мышь и колесо на сцене.
  useEffect(() => {
    if (!stage) return;
    let drag: { x: number; y: number; id: number } | null = null;

    const onDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, id: e.pointerId };
      stage.setPointerCapture(e.pointerId);
      stage.classList.add('sb__stage--drag');
    };
    const onMove = (e: PointerEvent) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      drag.x = e.clientX;
      drag.y = e.clientY;
      const c = camRef.current;
      if (e.shiftKey) {
        // Сдвиг: чем ближе камера, тем мельче шаг, чтобы Юми шла за мышью.
        const k = 0.0025 / c.zoom;
        apply({ offsetX: clamp('offsetX', c.offsetX + dx * k), offsetY: clamp('offsetY', c.offsetY - dy * k) });
      } else {
        apply({ yaw: clamp('yaw', c.yaw - dx * 0.4), pitch: clamp('pitch', c.pitch + dy * 0.3) });
      }
    };
    const onUp = (e: PointerEvent) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      stage.releasePointerCapture(e.pointerId);
      stage.classList.remove('sb__stage--drag');
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const c = camRef.current;
      apply({ zoom: clamp('zoom', c.zoom * Math.exp(-e.deltaY * 0.0012)) });
    };

    stage.addEventListener('pointerdown', onDown);
    stage.addEventListener('pointermove', onMove);
    stage.addEventListener('pointerup', onUp);
    stage.addEventListener('pointercancel', onUp);
    // passive: false — иначе браузер прокрутит страницу вместо зума.
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      stage.removeEventListener('pointerdown', onDown);
      stage.removeEventListener('pointermove', onMove);
      stage.removeEventListener('pointerup', onUp);
      stage.removeEventListener('pointercancel', onUp);
      stage.removeEventListener('wheel', onWheel);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage]);

  /** Записать в файл; при ошибке на экране остаётся прежнее. */
  async function post(route: string, body: unknown, message: string) {
    try {
      const res = await fetch(route, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setStatus({ ok: true, text: message });
      return true;
    } catch {
      setStatus({
        ok: false,
        text: 'Не сохранилось: запись в файл работает только из npm run dev.',
      });
      return false;
    }
  }

  async function saveAll() {
    if (await post('/mascot/camera', cam, 'Сохранено: так Юми будет стоять в кадре по умолчанию.')) setSaved(cam);
  }

  async function saveClips(message: string) {
    const all = renderer()?.getClipCameras() ?? liveClips;
    if (await post('/mascot/clip-cameras', all, message)) {
      setSavedClips(all);
      setLiveClips(all);
    }
  }

  function resetClip(name: string) {
    renderer()?.setClipCamera(name, null);
    setLiveClips(renderer()?.getClipCameras() ?? {});
    if (name === clip && scope === 'clip') {
      const r = renderer();
      if (r) setCam(r.getCamera());
    }
  }

  const dirtyAll = JSON.stringify(cam) !== JSON.stringify(saved);
  const dirtyClips = JSON.stringify(liveClips) !== JSON.stringify(savedClips);
  const ownClips = Object.keys(liveClips);

  return (
    <>
      <section>
        <h2>Что настраиваем</h2>
        <div className="sb__masks">
          <button
            className={scope === 'all' ? 'sb__chip sb__chip--on' : 'sb__chip'}
            onClick={() => setScope('all')}
          >
            все анимации
          </button>
          <button
            className={scope === 'clip' ? 'sb__chip sb__chip--on' : 'sb__chip'}
            onClick={() => setScope('clip')}
          >
            только текущую
          </button>
        </div>
        {scope === 'clip' && (
          <p className="sb__hint">
            {clip
              ? <>Сейчас играет: <b>{clipLabel(clip)}</b>{liveClips[clip] ? ' — свой кадр' : ' — общий кадр'}.
                {' '}Включите нужное состояние (лучше по кругу), и ползунки будут менять кадр только для него.</>
              : 'Анимация не играет — включите состояние или клип.'}
          </p>
        )}
      </section>

      <section>
        <h2>Кадр</h2>
        <div className="sb__masks">
          {FITS.map(([fit, label]) => (
            <button
              key={fit}
              className={cam.fit === fit ? 'sb__chip sb__chip--on' : 'sb__chip'}
              onClick={() => apply({ fit })}
              disabled={scope === 'clip' && !clip}
            >
              {label}
            </button>
          ))}
        </div>
      </section>

      <section>
        {SLIDERS.map(({ key, label, step, unit }) => (
          <div key={key}>
            <div className="sb__row">
              <span className="sb__knob">{label}</span>
              <span className="sb__val">{unit(cam[key])}</span>
            </div>
            <input
              className="sb__range" type="range"
              min={CAMERA_LIMITS[key][0]} max={CAMERA_LIMITS[key][1]} step={step}
              value={cam[key]}
              disabled={scope === 'clip' && !clip}
              onChange={(e) => apply({ [key]: Number(e.target.value) })}
              onDoubleClick={() => apply({ [key]: scope === 'clip' ? renderer()?.getCamera()[key] ?? CAMERA_DEFAULTS[key] : CAMERA_DEFAULTS[key] })}
              title={scope === 'clip' ? 'двойной клик — как в общем кадре' : 'двойной клик — значение по умолчанию'}
            />
          </div>
        ))}
        <p className="sb__hint">
          На сцене: колесо — приближение, перетаскивание — поворот,
          Shift + перетаскивание — сдвиг. Двойной клик по ползунку — сброс.
        </p>
      </section>

      {scope === 'all' ? (
        <section>
          <div className="sb__sayrow">
            <button className="sb__btn sb__btn--wide" disabled={!dirtyAll} onClick={() => apply(saved)}>
              к сохранённому
            </button>
            <button className="sb__btn sb__btn--wide" onClick={() => apply(CAMERA_DEFAULTS)}>
              исходный кадр
            </button>
          </div>
          <button
            className="sb__btn sb__btn--wide sb__btn--save"
            disabled={!dirtyAll}
            onClick={() => void saveAll()}
          >
            сохранить как вид по умолчанию
          </button>
        </section>
      ) : (
        <section>
          <button
            className="sb__btn sb__btn--wide"
            disabled={!clip || !liveClips[clip]}
            onClick={() => clip && resetClip(clip)}
          >
            как в общем кадре
          </button>
          <button
            className="sb__btn sb__btn--wide sb__btn--save"
            disabled={!dirtyClips}
            onClick={() => void saveClips('Сохранено: кадры анимаций.')}
          >
            сохранить кадры анимаций
          </button>
        </section>
      )}
      {status && (
        <p className={status.ok ? 'sb__hint sb__hint--ok' : 'sb__hint sb__hint--warn'}>{status.text}</p>
      )}

      <section>
        <h2>Анимации со своим кадром — {ownClips.length}</h2>
        {ownClips.map((name) => (
          <div key={name} className="sb__trow">
            <span className="sb__trow-pair" title={name}>{clipLabel(name)}</span>
            <span className="sb__val sb__val--muted">
              {Object.keys(liveClips[name]).length} настр.{savedClips[name] ? '' : ' · не сохранено'}
            </span>
            <button className="sb__icon" onClick={() => resetClip(name)} title="вернуть общий кадр">✕</button>
          </div>
        ))}
        {!ownClips.length && (
          <p className="sb__hint">Пока у всех анимаций общий кадр.</p>
        )}
      </section>

      {scope === 'all' && (
        <section>
          <div className="sb__row">
            <h2>Для кода</h2>
            <button className="sb__link" onClick={() => void navigator.clipboard.writeText(codeFor(cam))}>
              копировать
            </button>
          </div>
          <code className="sb__code">{codeFor(cam)}</code>
          <p className="sb__hint">
            Сохранённый вид действует во всех встраиваниях. Строка выше задаёт
            кадр для одного конкретного места — например, Юми в углу интерфейса.
          </p>
        </section>
      )}
    </>
  );
}
