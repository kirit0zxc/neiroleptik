// @vitest-environment happy-dom
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DataSourceBadge, DemoNote } from './DataSource';

afterEach(() => vi.unstubAllEnvs());

describe('источник данных: демо или центр', () => {
  it('демо: метка «демо-данные», пояснение и демо-входы', () => {
    vi.stubEnv('VITE_API_MOCK', '1');
    render(<><DataSourceBadge /><DemoNote logins /></>);
    expect(screen.getByText('демо-данные')).toBeInTheDocument();
    expect(screen.getByLabelText(/Источник данных: демо-данные/)).toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent('данные вымышленные');
    expect(screen.getByRole('note')).toHaveTextContent('viewer / viewer');
  });

  it('центр: метка «центр», демо-строки нет', () => {
    vi.stubEnv('VITE_API_MOCK', '0');
    render(<><DataSourceBadge /><DemoNote logins /></>);
    expect(screen.getByText('центр')).toBeInTheDocument();
    expect(screen.queryByText('демо-данные')).toBeNull();
    expect(screen.queryByRole('note')).toBeNull();
  });
});
