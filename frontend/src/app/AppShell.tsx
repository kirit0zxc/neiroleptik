import { useMutation, useQueryClient } from '@tanstack/react-query';
import { NavLink, Outlet } from 'react-router';
import { useMe } from '../features/auth/auth';
import { api, ApiError, setCsrfToken } from '../shared/api/client';
import { MascotSlot } from '../shared/mascot-slot/MascotSlot';
import { streamLabel, useStreamStatus } from '../shared/stream/stream';
import { StatusBadge } from '../shared/ui';
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

/** Каркас: шапка, навигация, контент и место под маскота. */
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
      <header className={s.header}>
        <span className={s.logo}>HoneyForge</span>
        <nav className={s.nav} aria-label="Разделы">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? s.active : undefined)}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        {me && (
          <div className={s.user}>
            <span aria-live="polite" title="Поток событий"><StatusBadge tone={live.tone}>{live.label}</StatusBadge></span>
            <span>{me.user.username} · {ROLE[me.user.role]}</span>
            <button className={s.logout} disabled={logout.isPending} onClick={() => logout.mutate()}>Выйти</button>
            {logoutFailed && <span role="alert" className={s.logoutError}>Не удалось выйти — попробуйте ещё раз</span>}
          </div>
        )}
      </header>
      <main className={s.main}><Outlet /></main>
      <MascotSlot />
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
