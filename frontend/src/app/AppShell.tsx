import { useMutation, useQueryClient } from '@tanstack/react-query';
import { NavLink, Outlet } from 'react-router';
import { useMe } from '../features/auth/auth';
import { api, ApiError, setCsrfToken } from '../shared/api/client';
import { MascotSlot, YumiConsole } from '../shared/mascot-slot/MascotSlot';
import { streamLabel, useStreamStatus } from '../shared/stream/stream';
import { StatusBadge } from '../shared/ui';
import { Terrain } from '../shared/ui/Terrain';
import { DataSourceBadge, DemoNote } from '../shared/ui/DataSource';
import { YumiEvents } from '../features/yumi/YumiEvents';
import s from './shell.module.css';

const NAV = [
  { to: '/', label: 'Обзор', end: true },
  { to: '/fleet', label: 'Парк' },
  { to: '/profiles', label: 'Профили' },
  { to: '/events', label: 'События' },
  { to: '/alerts', label: 'Алерты' },
  { to: '/audit', label: 'Аудит' },
];

const ROLE = { admin: 'администратор', operator: 'оператор', viewer: 'наблюдатель' } as const;

/** Каркас-«досье»: лист с шапкой, меню и Юми слева, контентом справа; под листом — консоль Юми. */
export function AppShell() {
  const me = useMe();
  const live = streamLabel(useStreamStatus());
  const qc = useQueryClient();
  // Сессию считаем закрытой только при успехе выхода или если сервер уже
  // не знает её (401). При другой ошибке (например, 503) серверная сессия
  // остаётся живой — не уводим на вход, чтобы это не выглядело успешным выходом.
  const endSession = () => { setCsrfToken(null); qc.setQueryData(['me'], null); };
  const logout = useMutation({
    mutationFn: () => api('/api/v1/auth/logout', { method: 'POST' }),
    onSuccess: endSession,
    onError: (err) => { if (err instanceof ApiError && err.status === 401) endSession(); },
  });
  const logoutFailed = logout.isError && !(logout.error instanceof ApiError && logout.error.status === 401);

  return (
    <div className={s.shell}>
      <Terrain />
      <div className={s.sheet}>
        <header className={s.header}>
          <span className={s.logo}>HoneyForge</span>
          {me && (
            <div className={s.user}>
              <DataSourceBadge />
              <span aria-live="polite" title="Поток событий"><StatusBadge tone={live.tone}>{live.label}</StatusBadge></span>
              <span>{me.user.username} · {ROLE[me.user.role]}</span>
              <button className={s.logout} disabled={logout.isPending} onClick={() => logout.mutate()}>Выйти</button>
              {logoutFailed && <span role="alert" className={s.logoutError}>Не удалось выйти — попробуйте ещё раз</span>}
            </div>
          )}
        </header>
        <aside className={s.side} aria-label="Навигация и Юми">
          <nav className={s.nav} aria-label="Разделы">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? s.active : undefined)}>
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className={s.mascot}><MascotSlot /></div>
        </aside>
        <main className={s.main}><DemoNote /><Outlet /></main>
      </div>
      {me && <YumiEvents username={me.user.username} />}
      <YumiConsole />
    </div>
  );
}

/** Экран, который появится по плану позже — честно говорим об этом. */
export function Planned({ title, day }: { title: string; day: string }) {
  return (
    <>
      <h1>{title}</h1>
      <p className={s.planned}>Экран в работе — по плану {day}.</p>
    </>
  );
}

export function NotFound() {
  return <><h1>404</h1><p className={s.planned}>Такой страницы нет.</p></>;
}
