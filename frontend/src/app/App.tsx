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

/** Телеметрию включаем только для готового сервера или явно выбранных моков. */
function telemetryEnabled() {
  return import.meta.env.VITE_API_MOCK === '1' || import.meta.env.VITE_TELEMETRY_ENABLED === '1';
}

/** Каркас вошедшего оператора: поток событий живёт, пока открыта сессия. */
function LiveShell() {
  const create = useContext(StreamFactory);
  return telemetryEnabled() ? <StreamProvider create={create}><AppShell /></StreamProvider> : <AppShell />;
}

function HomePage() {
  return telemetryEnabled() ? <OverviewPage /> : <Navigate to="/fleet" replace />;
}

function EventsScreen() {
  return telemetryEnabled() ? <EventsPage /> : <Planned title="События" day="D3 — подключение серверной телеметрии" />;
}

export const routes = [
  { path: '/login', element: <LoginPage /> },
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
      { path: 'sessions/:id', element: <Planned title="Паспорт атаки" day="D3" /> },
      { path: 'alerts', element: <Planned title="Алерты и экспорт" day="D3" /> },
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
  const [operations] = useState(() => new Operations(() => void client.invalidateQueries({ queryKey: ['deployments'] })));
  return (
    <QueryClientProvider client={client}>
      <StreamFactory.Provider value={createStream}>
        <OperationsContext.Provider value={operations}>
          <AuthProvider>
            <RouterProvider router={router} />
          </AuthProvider>
        </OperationsContext.Provider>
      </StreamFactory.Provider>
    </QueryClientProvider>
  );
}
