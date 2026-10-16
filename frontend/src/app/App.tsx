import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createContext, useContext, useState } from 'react';
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router';
import { AuthProvider, LoginPage, RequireAuth } from '../features/auth/auth';
import { EventsPage } from '../features/events/EventsPage';
import { FleetPage } from '../features/fleet/FleetPage';
import { OverviewPage } from '../features/overview/OverviewPage';
import { LaunchWizard } from '../features/fleet/LaunchWizard';
import { ProfileEditor } from '../features/profiles/ProfileEditor';
import { ProfilesPage } from '../features/profiles/ProfilesPage';
import { ApiError } from '../shared/api/client';
import { createEventStream, StreamProvider, type EventStream } from '../shared/stream/stream';
import { Operations, OperationsContext } from '../features/fleet/operations';
import { AuditPage } from '../features/audit/AuditPage';
import { SessionPage } from '../features/replay/ReplayPage';
import { MascotCore } from '../mascot/core/MascotCore';
import { Yumi, YumiContext } from '../features/yumi/yumi';
import { YumiDemo } from '../features/yumi/YumiDemo';
import { analyticsEnabled, eventsEnabled, investigationEnabled, replayEnabled } from './telemetry';
import { InvestigationPage } from '../features/investigation/InvestigationPage';
import { AppShell, NotFound, Planned } from './AppShell';

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 4xx не повторяем — это не сбой сети, повтор не поможет.
        retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
        refetchOnWindowFocus: false,
      },
    },
  });
}

/** Откуда брать поток событий: настоящий WebSocket или подделка в тестах. */
const StreamFactory = createContext<() => EventStream>(createEventStream);

/** Каркас вошедшего оператора: поток событий живёт, пока открыта сессия. */
function LiveShell() {
  const create = useContext(StreamFactory);
  // Провайдер потока нужен ленте и живым событиям Юми — поднимаем его, когда
  // включена лента событий.
  return eventsEnabled() ? <StreamProvider create={create}><AppShell /></StreamProvider> : <AppShell />;
}

function HomePage() {
  return analyticsEnabled() ? <OverviewPage /> : <Navigate to="/fleet" replace />;
}

function SessionScreen() {
  return replayEnabled() ? <SessionPage /> : <Planned title="Запись сессии" day="D3 — подключение серверной телеметрии" />;
}

function InvestigationScreen() {
  return investigationEnabled() ? <InvestigationPage /> : <Planned title="Расследование" day="D3 — подключение серверных ручек расследования" />;
}

function EventsScreen() {
  return eventsEnabled() ? <EventsPage /> : <Planned title="События" day="D3 — подключение серверной телеметрии" />;
}

export const routes = [
  { path: '/login', element: <LoginPage /> },
  // Витрина реакций Юми — только для разработки (ручной просмотр), в прод-сборку
  // маршрут не попадает: не часть продукта и без входа.
  ...(import.meta.env.DEV ? [{ path: '/yumi-demo', element: <YumiDemo /> }] : []),
  {
    element: <RequireAuth><LiveShell /></RequireAuth>,
    children: [
      { index: true, element: <HomePage /> },
      { path: 'fleet', element: <FleetPage /> },
      { path: 'fleet/new', element: <LaunchWizard /> },
      { path: 'profiles', element: <ProfilesPage /> },
      { path: 'profiles/new', element: <ProfileEditor /> },
      { path: 'profiles/:id', element: <ProfileEditor /> },
      { path: 'events', element: <EventsScreen /> },
      { path: 'sessions/:id', element: <SessionScreen /> },
      { path: 'alerts', element: <InvestigationScreen /> },
      { path: 'audit', element: <AuditPage /> },
      { path: '*', element: <NotFound /> },
    ],
  },
];

export function App({ client = makeQueryClient(), createStream = createEventStream }: {
  client?: QueryClient; createStream?: () => EventStream;
}) {
  const router = createBrowserRouter(routes);
  // Операции с ловушками живут на уровне приложения: переход между экранами
  // и обновление списка их не теряют.
  // Юми — своё ядро на экземпляр приложения (без общего синглтона): тесты и
  // повторные монтирования не делят состояние.
  const [yumi] = useState(() => new Yumi(new MascotCore()));
  const [operations] = useState(() => new Operations(
    () => void client.invalidateQueries({ queryKey: ['deployments'] }),
    (event, payload) => yumi.handle(event, payload),
  ));
  return (
    <QueryClientProvider client={client}>
      <StreamFactory.Provider value={createStream}>
        <YumiContext.Provider value={yumi}>
        <OperationsContext.Provider value={operations}>
          <AuthProvider>
            <RouterProvider router={router} />
          </AuthProvider>
        </OperationsContext.Provider>
        </YumiContext.Provider>
      </StreamFactory.Provider>
    </QueryClientProvider>
  );
}
