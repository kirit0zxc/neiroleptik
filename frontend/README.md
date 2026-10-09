# HoneyForge — frontend

Пульт оператора ловушек. Требования, экраны и критерии приёмки —
[`../specs/frontend.md`](../specs/frontend.md); код пишется по ней.

## Запуск

```bash
npm ci
cp .env.example .env      # VITE_API_MOCK=1 — работать на моках без backend
npm run dev               # http://127.0.0.1:5173
```

На моках вход: `operator` / `operator`. С настоящим backend уберите
`VITE_API_MOCK`, а адрес API задайте в `VITE_API_PROXY` (по умолчанию
`http://127.0.0.1:8000`) — dev-сервер проксирует `/api` и WebSocket.

## Проверки

| Команда | Что делает |
|---|---|
| `npm run lint` | oxlint |
| `npm run typecheck` | `tsc -b` |
| `npm run test:ci` | unit и компонентные тесты (Vitest + Testing Library + MSW) |
| `npm run e2e` | E2E (Playwright) на моках; локально — установленный Edge |
| `npm run build` | production-сборка |

E2E на реальном стенде: `E2E_BASE_URL=https://… npm run e2e`.
В CI без Edge: `PW_CHANNEL=chromium` и `npx playwright install chromium`.

## Структура

```
src/app/          маршруты, провайдеры, каркас, тема
src/features/     auth, fleet, events, … — экраны по разделам спецификации
src/shared/       API-клиент и типы, поток событий, безопасный текст, UI, место под маскота
src/mocks/        MSW по фикстурам из ../contracts/fixtures
e2e/              Playwright
```

Типы control API в `src/shared/api/types.ts` соответствуют общему OpenAPI.
Типы команд и телеметрии пока описывают превью следующих этапов.
