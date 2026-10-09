// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest';
import type { PreviewDeployment } from '../../shared/api/types';
import { availableCommands, isFinal, jobLabel } from './commands';

const d = (patch: Partial<PreviewDeployment>): PreviewDeployment => ({
  id: 'x', host_id: 'h', interaction_level: 'medium', state: 'running',
  profile_id: 'p', desired_revision: 1, applied_revision: 1, ...patch,
});

describe('доступность команд (F6)', () => {
  it('работающая Medium без drift: стоп и смена учёток', () => {
    expect(availableCommands(d({}))).toEqual(['stop', 'rotate_credentials']);
  });

  it('drift добавляет «применить профиль»', () => {
    expect(availableCommands(d({ applied_revision: 1, desired_revision: 2 }))).toContain('apply_config');
  });

  it('Low не меняет учётки и не сбрасывается', () => {
    expect(availableCommands(d({ interaction_level: 'low' }))).toEqual(['stop']);
  });

  it('reset — только High, работающей или упавшей', () => {
    expect(availableCommands(d({ interaction_level: 'high' }))).toContain('reset');
    expect(availableCommands(d({ interaction_level: 'high', state: 'failed' }))).toEqual(['start', 'reset']);
    expect(availableCommands(d({ interaction_level: 'high', state: 'stopped' }))).toEqual(['start']);
    expect(availableCommands(d({ interaction_level: 'medium', state: 'failed' }))).not.toContain('reset');
  });

  it('в переходных состояниях команд нет', () => {
    expect(availableCommands(d({ state: 'deploying' }))).toEqual([]);
    expect(availableCommands(d({ state: 'stopping' }))).toEqual([]);
  });
});

describe('ход задания', () => {
  it('итоговые состояния', () => {
    expect(['succeeded', 'failed', 'expired'].every((s) => isFinal(s as never))).toBe(true);
    expect(['requested', 'leased', 'running'].some((s) => isFinal(s as never))).toBe(false);
  });

  it('подписи с текстом ошибки', () => {
    expect(jobLabel({ state: 'leased', error: null })).toEqual({ tone: 'warn', label: 'взято агентом' });
    expect(jobLabel({ state: 'failed', error: 'нет места на диске' }).label).toBe('ошибка: нет места на диске');
    expect(jobLabel({ state: 'expired', error: null }).tone).toBe('bad');
  });
});
