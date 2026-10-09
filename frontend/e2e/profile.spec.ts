import { expect, test } from '@playwright/test';

/** Профиль ловушки (F8, F9): создать Medium с приманкой → ревизия r1 → правка → r2. */
test('создание профиля и новая ревизия', async ({ page }) => {
  await page.goto('/profiles/new');
  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  await expect(page.getByRole('heading', { name: 'Новый профиль' })).toBeVisible();
  await page.getByLabel('Название').fill('SSH-приманка склада');
  await page.getByLabel('Уровень взаимодействия').selectOption('medium');
  await page.getByRole('button', { name: '+ Учётка' }).click();
  const cred = page.getByRole('group', { name: 'Учётка 1' });
  await cred.getByLabel('Логин').fill('backup');
  await cred.getByLabel('Пароль').fill('Backup2024!');
  await page.getByRole('button', { name: 'Создать профиль' }).click();

  await expect(page.getByRole('status')).toHaveText('Сохранено: ревизия r1');
  await expect(page).toHaveURL(/\/profiles\/p-new-1$/);

  // Правка канала сохраняет скрытые пароли без повторного ввода.
  await page.getByLabel('Размер пачки').fill('50');
  await page.getByRole('button', { name: 'Сохранить новую ревизию' }).click();
  await expect(page.getByRole('status')).toHaveText('Сохранено: ревизия r2');

  await page.getByRole('link', { name: '← Все профили' }).click();
  await expect(page.getByRole('row').filter({ hasText: 'SSH-приманка склада' })).toContainText('r2');
});
