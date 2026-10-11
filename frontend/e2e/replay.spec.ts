import { expect, test } from '@playwright/test';

/**
 * Лента → «Запись» → replay (F15, S2) на моках: запись открывается из ленты,
 * текст без управляющих последовательностей, гиперссылка OSC 8 не стала
 * ссылкой, разрыв данных отмечен. reduced-motion — без автопуска.
 */
test.use({ reducedMotion: 'reduce' });

test('из ленты событий к записи сессии', async ({ page }) => {
  let dialog = false;
  page.on('dialog', async (d) => { dialog = true; await d.dismiss(); });

  await page.goto('/events');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  await page.getByRole('link', { name: /^Запись сессии …/ }).first().click();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}$/);
  const screen = page.getByRole('region', { name: 'Текст записи' });

  // reduced-motion: запись загружена целиком (длительность известна), но стоим в начале.
  await expect(page.getByText('00:00 / 00:11')).toBeVisible();
  await page.waitForTimeout(1500);
  await expect(page.getByRole('button', { name: 'Пуск' })).toBeVisible();
  await expect(page.getByText('00:00 / 00:11')).toBeVisible();
  await expect(screen).not.toContainText('whoami');

  await page.getByRole('button', { name: 'В конец' }).click();
  await expect(screen).toContainText('whoami');
  await expect(screen).toContainText('Сохранение в: «/tmp/x.sh»');
  await expect(screen).toContainText('[пропуск данных]');
  await expect(screen).toContainText('ссылка загружена');
  await expect(screen).toContainText('logout');
  // S2: ни управляющих символов, ни ссылок из OSC 8.
  expect(await screen.evaluate((el) => el.textContent?.includes('\u001b'))).toBe(false);
  await expect(screen.getByRole('link')).toHaveCount(0);

  await page.getByRole('button', { name: 'В начало' }).click();
  await expect(screen).not.toContainText('whoami');
  expect(dialog).toBe(false);
});
