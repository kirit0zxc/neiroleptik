import { expect, test } from '@playwright/test';

/** viewer не меняет данные (F2): кнопок изменения нет, прямой запрос — 403. */
test('наблюдатель видит парк и профили без кнопок изменения', async ({ page }) => {
  await page.goto('/fleet');
  await page.getByLabel('Логин').fill('viewer');
  await page.getByLabel('Пароль').fill('viewer');
  await page.getByRole('button', { name: 'Войти' }).click();

  const low = page.getByRole('row').filter({ hasText: 'decoy-low-01' });
  await expect(low.getByText('работает')).toBeVisible();
  await expect(page.getByRole('link', { name: '+ Запустить ловушку' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '+ Добавить хост' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^(Остановить|Запустить|Сбросить|Отозвать)/ })).toHaveCount(0);

  await page.getByRole('link', { name: 'Профили' }).click();
  await expect(page.getByRole('heading', { name: 'Профили ловушек', level: 1 })).toBeVisible();
  await expect(page.getByRole('link', { name: '+ Новый профиль' })).toHaveCount(0);

  // Запрет определяет API, а не скрытые кнопки.
  const status = await page.evaluate(async () => (await fetch('/api/v1/deployments/decoy-low-01/commands', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'mock-csrf-token' },
    body: JSON.stringify({ command: 'stop', idempotency_key: crypto.randomUUID(), expected_generation: 1 }),
  })).status);
  expect(status).toBe(403);
});
