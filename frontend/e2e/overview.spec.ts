import { expect, test } from '@playwright/test';

/** Обзор: плитки сводки и живые «Последние события» после входа. */
test('обзор после входа: плитки и живые события', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  await expect(page.getByRole('heading', { name: 'Обзор' })).toBeVisible();
  await expect(page.getByRole('link', { name: /^Ловушки: 2 из 3 работают/ })).toBeVisible();
  // Демо-атака приходит по потоку и встаёт первой.
  await expect(page.getByTestId('recent-row').first()).toContainText('203.0.113.50', { timeout: 10_000 });
});
