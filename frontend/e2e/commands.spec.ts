import { expect, test } from '@playwright/test';

/** Команды ловушки (F6): остановить Low с подтверждением → «остановлена» → запустить → «работает». */
test('остановка и запуск ловушки с ходом задания', async ({ page }) => {
  await page.goto('/fleet');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  const low = page.getByRole('row').filter({ hasText: 'decoy-low-01' });
  await expect(low.getByText('работает')).toBeVisible();

  await low.getByRole('button', { name: 'Остановить' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('перестанет принимать подключения');
  await dialog.getByRole('button', { name: 'Подтвердить' }).click();

  await expect(low.getByRole('status')).toContainText('Остановить: выполнено', { timeout: 10_000 });
  await expect(low.getByText('остановлена')).toBeVisible();

  await low.getByRole('button', { name: 'Запустить' }).click();
  await expect(low.getByRole('status')).toContainText('Запустить: выполнено', { timeout: 10_000 });
  await expect(low.getByText('работает')).toBeVisible();
});
