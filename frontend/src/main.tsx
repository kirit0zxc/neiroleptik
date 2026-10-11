import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './app/App';
import './app/theme.css';

async function start() {
  // Без backend работаем на моках MSW (VITE_API_MOCK=1, см. .env.example).
  if (import.meta.env.VITE_API_MOCK === '1') {
    // Скриншоты демо не перепутать с центром (specs, «Источник данных»).
    document.title = `[демо] ${document.title}`;
    const { worker, startDemoAttacks } = await import('./mocks/browser');
    await worker.start({ onUnhandledRequest: 'bypass' });
    startDemoAttacks();
  }
  createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
}

void start();
