import { expect, test } from '@playwright/test';

/** Мастер запуска (F7): High на хосте с KVM → задание → новая ловушка «работает» в парке. */
test('запуск High-ловушки через мастер', async ({ page }) => {
  await page.goto('/fleet');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  await page.getByRole('link', { name: '+ Запустить ловушку' }).click();
  await page.getByRole('radio', { name: /host-runtime-02/ }).check();
  await page.getByRole('button', { name: 'Далее' }).click();
  await page.getByRole('radio', { name: /Настоящая Linux VM/ }).check();
  await page.getByRole('button', { name: 'Далее' }).click();
  await expect(page.getByText('Настоящая Linux VM · ревизия r1')).toBeVisible();
  await page.getByRole('button', { name: 'Запустить' }).click();

  await expect(page.getByText(/запущена/)).toBeVisible({ timeout: 10_000 });
  await page.getByRole('link', { name: 'Перейти в парк' }).click();
  const created = page.getByRole('row').filter({ hasText: 'decoy-high-04' });
  await expect(created.getByText('работает')).toBeVisible();
});
