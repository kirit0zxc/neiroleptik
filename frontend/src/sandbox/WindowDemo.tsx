import { useEffect, useState } from 'react';
import type { VrmRenderer } from '../mascot/renderers/vrm/VrmRenderer';
import { BrowserTTS } from '../mascot/voice/BrowserTTS';
import { ServerTTS } from '../mascot/voice/ServerTTS';
import { MascotWindow } from '../mascot/react/window/MascotWindow';
import { useMascot } from '../mascot/react/useMascot';
import { STATES } from '../mascot/core/states';
import './windowDemo.css';

/**
 * Демо встраивания: условный интерфейс приложения и Юми в окне поверх
 * него. Справа — пульт, чтобы двигать её так, как это будет делать
 * приложение: состояния и реплики.
 */
export function WindowDemo() {
  const { state, set, say, core } = useMascot();
  const diag = useDiagnostics(core);
  const [phrase, setPhrase] = useState('Привет! Я Юми, буду рядом.');
  const names = Object.keys(STATES).filter((n) => !n.startsWith('__'));
  useEffect(() => {
    const provider = import.meta.env.VITE_TTS_PROVIDER === 'browser' ? new BrowserTTS() : new ServerTTS();
    core.setVoiceProvider(provider);
    return () => provider.cancel();
  }, [core]);

  return (
    <div className="wd">
      <header className="wd__top">
        <strong>neiroleptik</strong>
        <nav>
          <span>Данные</span><span>Модели</span><span>Результаты</span>
        </nav>
        <a href="?view=lab" className="wd__back">лаборатория →</a>
      </header>

      <main className="wd__main">
        <section className="wd__card wd__card--wide">
          <h2>Загрузка данных</h2>
          <p>Здесь будет интерфейс приложения. Окно Юми живёт поверх него слева:
            его можно перетащить за заголовок, свернуть, вынести в отдельное окно.</p>
          <div className="wd__bar"><span style={{ width: '64%' }} /></div>
        </section>
        <section className="wd__card"><h2>Точность</h2><p className="wd__big">0.87</p></section>
        <section className="wd__card"><h2>Запусков</h2><p className="wd__big">12</p></section>
        <section className="wd__card wd__card--wide">
          <h2>Таблица</h2>
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="wd__row"><span>запись {i + 1}</span><span>{(0.5 + i * 0.09).toFixed(2)}</span></div>
          ))}
        </section>
      </main>

      <aside className="wd__remote">
        <h3>Пульт приложения</h3>
        <p className="wd__muted">сейчас: {state}</p>
        <dl className="wd__diag">
          <dt>время анимации</dt><dd>{diag.elapsed}</dd>
          <dt>Юми рисуется</dt><dd>{diag.drawing}</dd>
          <dt>отдельное окно</dt><dd>{diag.pip}</dd>
          <dt>голос</dt><dd>{diag.voice}</dd>
        </dl>
        <div className="wd__states">
          {names.map((n) => (
            <button key={n} className={n === state ? 'wd__on' : ''} onClick={() => set(n)}>{n}</button>
          ))}
        </div>
        <textarea value={phrase} rows={2} onChange={(e) => setPhrase(e.target.value)} />
        <button onClick={() => say(phrase)}>сказать</button>
      </aside>

      <MascotWindow />
    </div>
  );
}

/**
 * Диагностика для ручной проверки в Chrome: растёт ли время анимации
 * (значит, Юми рисуется), открыто ли отдельное окно, какой голос выбран.
 * Опрос по таймеру, а не по кадрам: так видно и паузу отрисовки.
 */
function useDiagnostics(core: ReturnType<typeof useMascot>['core']) {
  const [diag, setDiag] = useState({ elapsed: '—', drawing: '—', pip: '—', voice: '…' });
  useEffect(() => {
    let voice = '…';
    if (import.meta.env.VITE_TTS_PROVIDER !== 'browser') voice = 'локальный голос Юми';
    else void BrowserTTS.loadVoices().then((list) => {
        voice = BrowserTTS.choose(list)?.name ?? 'голосов нет';
      });
    const id = setInterval(() => {
      const r = core.getRenderer() as VrmRenderer | null;
      const s = r?.stats?.();
      const pipApi = (window as unknown as { documentPictureInPicture?: { window: Window | null } }).documentPictureInPicture;
      setDiag({
        elapsed: s ? `${s.elapsed.toFixed(1)} с` : 'нет картинки',
        drawing: s ? (s.drawing ? 'да' : 'на паузе') : '—',
        pip: !pipApi ? 'не поддерживается' : pipApi.window ? 'открыто' : 'закрыто',
        voice,
      });
    }, 250);
    return () => clearInterval(id);
  }, [core]);
  return diag;
}
