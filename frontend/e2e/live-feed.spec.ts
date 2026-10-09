import { expect, test } from '@playwright/test';

/**
 * Живая лента (F10, F12) в настоящем браузере: в режиме моков MSW по
 * WebSocket каждые 4 с присылает демо-атаку — она появляется сверху
 * без перезагрузки, индикатор показывает «онлайн».
 */
test('живое событие приходит по WebSocket и встаёт сверху', async ({ page }) => {
  await page.goto('/events');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  await expect(page.getByText('онлайн')).toBeVisible();
  const before = await page.getByTestId('event-row').count();
  // Первая демо-атака — подключение к Medium с адреса 203.0.113.50.
  await expect(page.getByTestId('event-row').first()).toContainText('203.0.113.50', { timeout: 10_000 });
  expect(await page.getByTestId('event-row').count()).toBeGreaterThan(before);
});

test('фильтр по типу: в адресе страницы и после перезагрузки', async ({ page }) => {
  await page.goto('/events');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();
  await page.getByTestId('event-row').first().waitFor();

  await page.getByLabel('Тип').selectOption('auth.attempt');
  await page.getByRole('button', { name: 'Применить' }).click();
  await expect(page).toHaveURL(/type=auth.attempt/);
  await page.getByTestId('event-row').first().waitFor();
  for (const row of await page.getByTestId('event-row').all()) await expect(row).toContainText('Попытка входа');

  // После перезагрузки фильтр берётся из адреса.
  await page.reload();
  await expect(page.getByLabel('Тип')).toHaveValue('auth.attempt');
  await page.getByTestId('event-row').first().waitFor();
  for (const row of await page.getByTestId('event-row').all()) await expect(row).toContainText('Попытка входа');
});
