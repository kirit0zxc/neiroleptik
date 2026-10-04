import { lazy, Suspense } from 'react';
import { WindowDemo } from './sandbox/WindowDemo';

// Лаборатория нужна только для настройки Юми — грузим её отдельным
// чанком, чтобы она не утяжеляла стартовую страницу.
const Sandbox = lazy(() => import('./sandbox/Sandbox').then((m) => ({ default: m.Sandbox })));

/** ?view=lab — лаборатория анимаций, иначе страница приложения с окном Юми. */
export default function App() {
  const view = new URLSearchParams(window.location.search).get('view');
  return view === 'lab'
    ? <Suspense fallback={null}><Sandbox /></Suspense>
    : <WindowDemo />;
}
