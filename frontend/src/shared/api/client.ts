/**
 * Клиент API (specs/frontend.md, раздел 4): cookie-сессия `hf_session`,
 * CSRF-токен из ответа входа и /auth/me — только в памяти, заголовок
 * `X-CSRF-Token` на изменяющих запросах; ошибки — контейнер `detail`.
 */

/** Ошибка поля из 422: путь без служебного `body`, текст сервера. */
export interface FieldIssue { path: (string | number)[]; message: string }

export class ApiError extends Error {
  readonly status: number;
  /** Ошибки полей для 422; пусто для остальных статусов. */
  readonly fields: FieldIssue[];
  /** Машинный код бизнес-ошибки (`ErrorResponse.code`), например STALE_GENERATION. */
  readonly code: string | null;

  constructor(status: number, message: string, fields: FieldIssue[] = [], code: string | null = null) {
    super(message);
    this.status = status;
    this.fields = fields;
    this.code = code;
  }
}

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

let csrfToken: string | null = null;
/** Запомнить CSRF-токен из ответа входа или /auth/me (в памяти, не в storage). */
export function setCsrfToken(token: string | null) { csrfToken = token; }

/** Вызывается на 401 — приложение уводит на вход. */
let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) { onUnauthorized = fn; }
export function notifyUnauthorized() { onUnauthorized(); }

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  if (UNSAFE.has(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken);

  let res: Response;
  try {
    res = await fetch(path, { ...init, method, headers, credentials: 'include' });
  } catch {
    throw new ApiError(0, 'Нет связи с сервером');
  }

  // 401 при входе — неверный пароль, а не истёкшая сессия (F3).
  if (res.status === 401 && !path.startsWith('/api/v1/auth/login')) onUnauthorized();
  if (!res.ok) throw await errorFrom(res);
  if (res.status === 204) return undefined as T;
  // Обрыв тела успешного ответа: сервер мог выполнить запрос (например, принять
  // команду), но ответ потерян — результат неизвестен, как при сбое сети.
  let text: string;
  try {
    text = await res.text();
  } catch {
    throw new ApiError(0, 'Ответ сервера получен не полностью — результат неизвестен');
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    // Тело получено целиком, но это не JSON. Изменяющий запрос мог выполниться —
    // результат неизвестен; чтение (например, страница прокси вместо API) —
    // отказ, а не бесконечный повтор.
    if (UNSAFE.has(method)) throw new ApiError(0, 'Ответ сервера получен не полностью — результат неизвестен');
    throw new ApiError(res.status, 'Сервер вернул непонятный ответ', [], 'BAD_RESPONSE');
  }
}

/**
 * Ошибка из ответа. Логика экранов опирается на статус; текст `detail`
 * только показывается. 422 — список `{loc, msg}` раскладывается по полям.
 */
export async function errorFrom(res: Response): Promise<ApiError> {
  let detail: unknown;
  let code: string | null = null;
  try {
    const body = (await res.json()) as { detail?: unknown; code?: unknown };
    detail = body.detail;
    if (typeof body.code === 'string' && body.code) code = body.code;
  } catch { /* тело не JSON */ }

  if (res.status === 422 && Array.isArray(detail)) {
    const fields = detail.flatMap((d): FieldIssue[] => {
      const item = d as { loc?: unknown; msg?: unknown };
      if (!Array.isArray(item.loc) || typeof item.msg !== 'string') return [];
      const path = item.loc.filter((p): p is string | number => typeof p === 'string' || typeof p === 'number');
      return [{ path: path[0] === 'body' ? path.slice(1) : path, message: item.msg }];
    });
    return new ApiError(422, 'Проверьте выделенные поля', fields);
  }
  return new ApiError(res.status, typeof detail === 'string' && detail ? detail : defaultMessage(res.status), [], code);
}

function defaultMessage(status: number) {
  if (status === 401) return 'Сессия истекла — войдите снова';
  if (status === 403) return 'Недостаточно прав для этого действия';
  if (status === 404) return 'Не найдено';
  if (status === 409) return 'Данные изменились — перечитайте их';
  if (status >= 500) return 'Ошибка сервера — попробуйте позже';
  return 'Запрос не выполнен';
}
