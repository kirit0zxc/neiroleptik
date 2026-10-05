# neiroleptik
Hackathon project: React + TypeScript, Python/Django, PostgreSQL, and machine learning.

## Frontend

`frontend/` — React + TypeScript (Vite). The start page is a stub of the
app with the Yumi mascot window; `?view=lab` opens the mascot lab.

```bash
cd frontend
npm ci
npm run dev
```

Specs: `specs/mascot.md` (mascot module), `specs/architecture.md`.

## Backend voice

Django + local Piper TTS is connected to the start page. Setup and commands:
[backend/README.md](backend/README.md). Run Django on port 8000 alongside
Vite; the development proxy sends /api to it. API: [specs/openapi.json](specs/openapi.json).
Voice models stay outside Git. The mascot lab keeps browser speech.
