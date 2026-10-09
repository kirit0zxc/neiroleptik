import type { ReactNode } from 'react';
import s from './ui.module.css';

export type Tone = 'ok' | 'warn' | 'bad' | 'idle';

/** Состояние: цвет всегда вместе с текстом (specs, раздел 7). */
export function StatusBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`${s.badge} ${s[tone]}`}>{children}</span>;
}

/** Панель — ретро-окно в стиле окна Юми (specs, 6a). */
export function Panel({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <section className="win" aria-label={title}>
      {title && <WindowTitle title={title} />}
      <div className="win-body">{children}</div>
    </section>
  );
}

/** Заголовок-полоска окна: пиктограмма, название, декоративные «кнопки». */
export function WindowTitle({ title, id }: { title: string; id?: string }) {
  return (
    <div className="win-titlebar">
      <span className="win-icon" aria-hidden="true" />
      <h2 id={id}>{title}</h2>
      <span className="win-controls" aria-hidden="true"><span /><span /></span>
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <div className={s.state} role="status">Загружаем {what}…</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className={s.state}>{children}</div>;
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className={s.state} role="alert">
      {message}
      {onRetry && <div><button className={s.button} onClick={onRetry}>Повторить</button></div>}
    </div>
  );
}

export const ui = s;
