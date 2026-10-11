import { expect, test } from '@playwright/test';

/** Команды ловушки (F6): остановить Low с подтверждением → «остановлена» → запустить → «работает». */
test('остановка и запуск ловушки с ходом задания', async ({ page }) => {
  // Успешный сценарий не должен скрывать обрыв опроса за повторным запросом.
  const failedJobRequests: string[] = [];
  const sentCommands: string[] = [];
  page.on('requestfailed', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/v1/jobs/')) {
      failedJobRequests.push(request.failure()?.errorText ?? request.url());
    }
  });
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/commands')) {
      sentCommands.push((request.postDataJSON() as { command: string }).command);
    }
  });
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
  // После закрытия модального диалога фокус — на ходе задания, а не на body.
  await expect(low.getByRole('status')).toBeFocused();

  await expect(low.getByRole('status')).toContainText('Остановить: успешно', { timeout: 10_000 });
  await expect(low.getByText('остановлена')).toBeVisible();

  await low.getByRole('button', { name: 'Запустить' }).click();
  await expect(low.getByRole('status')).toContainText('Запустить: успешно', { timeout: 10_000 });
  await expect(low.getByText('работает')).toBeVisible();
  expect(failedJobRequests).toEqual([]);
  expect(sentCommands).toEqual(['stop', 'start']);
});
