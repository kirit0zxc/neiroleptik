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
  const rows = page.getByTestId('event-row');
  await rows.first().waitFor();
  const before = await rows.allTextContents();
  // Под нагрузкой первая демо-атака могла прийти ещё до snapshot. Ждём именно
  // новое событие сверху и рост списка, а не повторно узнаём старый IP.
  await expect.poll(async () => {
    const current = await rows.allTextContents();
    return current.length > before.length && !before.includes(current[0]!);
  }, { timeout: 12_000 }).toBe(true);
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
