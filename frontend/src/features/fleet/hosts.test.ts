import { describe, expect, it } from 'vitest';
import { canRevoke, ENROLL_COMMAND, EnrollmentTicket, minutesLabel } from './hosts';

const ticket = new EnrollmentTicket({ id: 'e1', enrollment_token: 'hf_enroll_x', expires_at: '2026-10-11T10:15:00Z' });
const at = (iso: string) => Date.parse(iso);

describe('регистрационный токен (F4)', () => {
  it('минуты до истечения с округлением вверх; после срока — истёк', () => {
    expect(ticket.minutesLeft(at('2026-10-11T10:00:00Z'))).toBe(15);
    expect(ticket.minutesLeft(at('2026-10-11T10:14:01Z'))).toBe(1);
    expect(ticket.expired(at('2026-10-11T10:14:59Z'))).toBe(false);
    expect(ticket.expired(at('2026-10-11T10:15:00Z'))).toBe(true);
    expect(ticket.minutesLeft(at('2026-10-11T10:20:00Z'))).toBe(0);
  });

  it('нераспознанный срок считается истёкшим', () => {
    expect(new EnrollmentTicket({ id: 'e', enrollment_token: 't', expires_at: 'мусор' }).expired(0)).toBe(true);
  });

  it('согласование минут', () => {
    expect([1, 2, 5, 11, 12, 15, 21, 22].map(minutesLabel)).toEqual([
      '1 минута', '2 минуты', '5 минут', '11 минут', '12 минут', '15 минут', '21 минута', '22 минуты',
    ]);
  });

  it('команда регистрации не содержит токен', () => {
    expect(ENROLL_COMMAND).toContain('agent.runtime.executor.enroll');
    expect(ENROLL_COMMAND).not.toContain('hf_enroll');
  });

  it('отозванный хост повторно не отзывается', () => {
    expect(canRevoke({ status: 'online' })).toBe(true);
    expect(canRevoke({ status: 'offline' })).toBe(true);
    expect(canRevoke({ status: 'revoked' })).toBe(false);
  });
});
