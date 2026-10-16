import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { YumiDemo } from './YumiDemo';

const yumi = vi.hoisted(() => ({ handle: vi.fn(), react: vi.fn(), cancelPendingAttack: vi.fn() }));
vi.mock('./yumi', () => ({ useYumi: () => yumi }));
vi.mock('../../shared/mascot-slot/MascotSlot', () => ({ MascotSlot: () => null, YumiConsole: () => null }));

afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

describe('Жизненный цикл dev-витрины', () => {
  it.each(['Проиграть всё', 'Реальный поток'])('останавливает сценарий %s при уходе', (label) => {
    vi.useFakeTimers();
    const view = render(<YumiDemo />);
    fireEvent.click(screen.getByRole('button', { name: new RegExp(label) }));
    act(() => vi.advanceTimersByTime(1600));
    expect(yumi.handle.mock.calls.length + yumi.react.mock.calls.length).toBeGreaterThan(0);
    view.unmount();
    const calls = yumi.handle.mock.calls.length + yumi.react.mock.calls.length;
    act(() => vi.advanceTimersByTime(30_000));
    expect(yumi.handle.mock.calls.length + yumi.react.mock.calls.length).toBe(calls);
    expect(yumi.cancelPendingAttack).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('не запускает второй сценарий одновременно и разрешает следующий после конца', () => {
    vi.useFakeTimers();
    render(<YumiDemo />);
    const live = screen.getByRole('button', { name: /Реальный поток/ });
    fireEvent.click(live);
    fireEvent.click(live);
    fireEvent.click(screen.getByRole('button', { name: /Проигрываю/ }));
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(5000));
    expect(yumi.react).toHaveBeenCalledTimes(9);
    expect(vi.getTimerCount()).toBe(0);
    fireEvent.click(live);
    expect(vi.getTimerCount()).toBe(1);
  });
});
