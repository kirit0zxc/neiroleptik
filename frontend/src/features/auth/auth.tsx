import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, useContext, useEffect, type ReactNode } from 'react';
import { useForm } from 'react-hook-form';
import { Navigate, useLocation, useNavigate } from 'react-router';
import { z } from 'zod';
import { api, ApiError, setCsrfToken, setUnauthorizedHandler } from '../../shared/api/client';
import type { Auth, Role } from '../../shared/api/types';
import { Loading, ui, WindowTitle } from '../../shared/ui';
import { Terrain } from '../../shared/ui/Terrain';
import s from './auth.module.css';

/** Сессия оператора (F1–F3). Источник — `GET /api/v1/auth/me` (AuthRead). */
const AuthContext = createContext<Auth | null>(null);
const ME_KEY = ['me'] as const;

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ME_KEY,
    queryFn: async () => {
      const me = await api<Auth>('/api/v1/auth/me');
      setCsrfToken(me.csrf_token);
      return me;
    },
    retry: false,
    staleTime: 60_000,
  });

  // 401 с любого запроса — сессии больше нет (F3).
  useEffect(() => {
    setUnauthorizedHandler(() => { setCsrfToken(null); qc.setQueryData(ME_KEY, null); });
  }, [qc]);

  if (me.isPending) return <Loading what="сессию" />;
  return <AuthContext.Provider value={me.data ?? null}>{children}</AuthContext.Provider>;
}

export function useMe() { return useContext(AuthContext); }

/** Может ли роль менять данные (F2); окончательно решает API. */
export function canEdit(role: Role | undefined) { return role === 'admin' || role === 'operator'; }

/** Закрытые страницы: без сессии — на вход, с запоминанием адреса (F1). */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const location = useLocation();
  if (!me) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return children;
}

const LoginSchema = z.object({
  username: z.string().trim().min(1, 'Введите логин'),
  password: z.string().min(1, 'Введите пароль'),
});
type LoginForm = z.infer<typeof LoginSchema>;

export function LoginPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const me = useMe();
  const from = (location.state as { from?: string } | null)?.from ?? '/';
  const form = useForm<LoginForm>({ resolver: zodResolver(LoginSchema), defaultValues: { username: '', password: '' } });
  const login = useMutation({
    mutationFn: (body: LoginForm) => api<Auth>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: (data) => {
      setCsrfToken(data.csrf_token);
      qc.setQueryData(ME_KEY, data);
      navigate(from, { replace: true });
    },
  });

  if (me) return <Navigate to={from} replace />;
  const { errors } = form.formState;
  const failure = login.error instanceof ApiError ? login.error.message : login.error ? 'Не удалось войти' : null;

  return (
    <main className={s.wrap}>
      <Terrain />
      <form className={`win ${s.card}`} onSubmit={form.handleSubmit((v) => login.mutate(v))} noValidate aria-labelledby="login-title">
        <WindowTitle title="honeyforge — вход" id="login-title" />
        <div className={s.inner}>
        <h1>HoneyForge</h1>
        <p className={s.sub}>Пульт оператора ловушек</p>
        {/* Ошибка — вне label: иначе она становится частью названия поля. */}
        <div className={s.field}>
          <label htmlFor="login">Логин</label>
          <input id="login" autoComplete="username" {...form.register('username')}
            aria-invalid={!!errors.username} aria-describedby={errors.username ? 'login-error' : undefined} />
          {errors.username && <span id="login-error" className={s.error}>{errors.username.message}</span>}
        </div>
        <div className={s.field}>
          <label htmlFor="password">Пароль</label>
          <input id="password" type="password" autoComplete="current-password" {...form.register('password')}
            aria-invalid={!!errors.password} aria-describedby={errors.password ? 'password-error' : undefined} />
          {errors.password && <span id="password-error" className={s.error}>{errors.password.message}</span>}
        </div>
        {failure && <p className={s.error} role="alert">{failure}</p>}
        <button className={ui.button} type="submit" disabled={login.isPending}>
          {login.isPending ? 'Входим…' : 'Войти'}
        </button>
        </div>
      </form>
    </main>
  );
}
