import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createContext, useContext } from 'react';
import { createBrowserRouter, RouterProvider } from 'react-router';
import { AuthProvider, LoginPage, RequireAuth } from '../features/auth/auth';
import { EventsPage } from '../features/events/EventsPage';
import { FleetPage } from '../features/fleet/FleetPage';
import { OverviewPage } from '../features/overview/OverviewPage';
import { LaunchWizard } from '../features/fleet/LaunchWizard';
import { ProfileEditor } from '../features/profiles/ProfileEditor';
import { ProfilesPage } from '../features/profiles/ProfilesPage';
import { ApiError } from '../shared/api/client';
import { createEventStream, StreamProvider, type EventStream } from '../shared/stream/stream';
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
  return <StreamProvider create={useContext(StreamFactory)}><AppShell /></StreamProvider>;
}

export const routes = [
  { path: '/login', element: <LoginPage /> },
  {
    element: <RequireAuth><LiveShell /></RequireAuth>,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: 'fleet', element: <FleetPage /> },
      { path: 'fleet/new', element: <LaunchWizard /> },
      { path: 'profiles', element: <ProfilesPage /> },
      { path: 'profiles/new', element: <ProfileEditor /> },
      { path: 'profiles/:id', element: <ProfileEditor /> },
      { path: 'events', element: <EventsPage /> },
      { path: 'sessions/:id', element: <Planned title="Паспорт атаки" day="D3" /> },
      { path: 'alerts', element: <Planned title="Алерты и экспорт" day="D3" /> },
      { path: 'audit', element: <Planned title="Аудит" day="D3" /> },
      { path: '*', element: <NotFound /> },
    ],
  },
];

export function App({ client = makeQueryClient(), createStream = createEventStream }: {
  client?: QueryClient; createStream?: () => EventStream;
}) {
  const router = createBrowserRouter(routes);
  return (
    <QueryClientProvider client={client}>
      <StreamFactory.Provider value={createStream}>
        <AuthProvider>
          <RouterProvider router={router} />
        </AuthProvider>
      </StreamFactory.Provider>
    </QueryClientProvider>
  );
}
