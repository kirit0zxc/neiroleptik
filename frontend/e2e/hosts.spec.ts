import { expect, test } from '@playwright/test';

/** Регистрация агента и отзыв хоста (F4) — только admin, на моках. */
test('администратор выдаёт токен регистрации и отзывает хост', async ({ page }) => {
  await page.goto('/fleet');
  await page.getByLabel('Логин').fill('admin');
  await page.getByLabel('Пароль').fill('admin');
  await page.getByRole('button', { name: 'Войти' }).click();

  await page.getByRole('button', { name: '+ Добавить хост' }).click();
  const enroll = page.getByRole('dialog', { name: 'Регистрация агента' });
  const token = enroll.getByRole('textbox', { name: 'Токен регистрации' });
  await expect(token).toHaveValue(/^hf_enroll_/);
  await expect(enroll).toContainText('осталось 15 минут');
  await expect(enroll).toContainText('HF_ENROLLMENT_TOKEN');
  const value = await token.inputValue();
  const command = enroll.getByLabel('Команда регистрации агента');
  await expect(command).toContainText('agent.runtime.executor.enroll');
  await expect(command).not.toContainText(value);
  await enroll.getByRole('button', { name: 'Закрыть' }).click();
  await expect(enroll).toBeHidden();
  // Токен не остаётся ни в адресе, ни на странице, ни в хранилищах.
  expect(page.url()).not.toContain('hf_enroll');
  await expect(page.locator('body')).not.toContainText(value);
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain('hf_enroll');

  const row = page.getByRole('row').filter({ hasText: 'runtime-03' });
  await row.getByRole('button', { name: 'Отозвать хост runtime-03' }).click();
  const confirm = page.getByRole('dialog', { name: 'Отозвать хост runtime-03?' });
  await expect(confirm).toContainText('Действие не отменить');
  await confirm.getByRole('button', { name: 'Подтвердить' }).click();
  await expect(row.getByText('отозван')).toBeVisible();
  await expect(row.getByRole('button', { name: /Отозвать/ })).toHaveCount(0);
  // Фокус не потерялся после закрытия модального окна.
  await expect(row).toBeFocused();
});
