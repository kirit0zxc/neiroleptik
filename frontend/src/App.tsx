import { Component, lazy, Suspense, type ReactNode } from 'react';
import { WindowDemo } from './sandbox/WindowDemo';

// Лаборатория нужна только для настройки Юми — грузим её отдельным
// чанком, чтобы она не утяжеляла стартовую страницу.
const Sandbox = lazy(() => import('./sandbox/Sandbox').then((m) => ({ default: m.Sandbox })));

/**
 * Чанк лаборатории не загрузился (сеть, новая выкладка) или она упала
 * при отрисовке — вместо пустой страницы сообщение и перезагрузка.
 */
class LabBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <p style={{ padding: 24, fontFamily: 'system-ui, sans-serif' }}>
        Лаборатория не загрузилась или упала.{' '}
        <button type="button" onClick={() => window.location.reload()}>Перезагрузить</button>
      </p>
    );
  }
}

/** ?view=lab — лаборатория анимаций, иначе страница приложения с окном Юми. */
export default function App() {
  const view = new URLSearchParams(window.location.search).get('view');
  return view === 'lab'
    ? <LabBoundary><Suspense fallback={null}><Sandbox /></Suspense></LabBoundary>
    : <WindowDemo />;
}
