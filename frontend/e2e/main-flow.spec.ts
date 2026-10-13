import { expect, test } from '@playwright/test';

/**
 * Главный путь оператора на моках (specs/frontend.md, раздел 8):
 * вход → парк (drift виден) → лента (XSS — текстом, пароль скрыт) → выход.
 * По мере появления экранов D2–D3 сценарий дополняется до
 * «профиль → ловушка → событие → паспорт → replay → reset».
 */
test('вход, парк, лента событий и выход', async ({ page }) => {
  // Срабатывание alert() — признак выполненного скрипта атакующего.
  let dialog = false;
  page.on('dialog', async (d) => { dialog = true; await d.dismiss(); });

  await page.goto('/events');
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();

  await page.getByLabel('Логин').fill('operator');
  await page.getByLabel('Пароль').fill('operator');
  await page.getByRole('button', { name: 'Войти' }).click();

  // Вернулись туда, куда шли.
  await expect(page.getByRole('heading', { name: 'События' })).toBeVisible();
  // Не меньше трёх: в режиме моков по потоку идут ещё и демо-атаки.
  await expect(page.getByTestId('event-row').nth(2)).toBeVisible();
  await expect(page.getByText('GET [скрыто] — полнота захвата неизвестна; ответ не записан')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('/<img src=x onerror=alert(1)>');
  await expect(page.locator('main img')).toHaveCount(0);
  await expect(page.getByText('root / пароль скрыт — успешно')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('hunter2');

  await page.getByRole('link', { name: 'Парк' }).click();
  const medium = page.getByRole('row').filter({ hasText: 'decoy-medium-01' });
  await expect(medium.getByText('drift: r3 → r4')).toBeVisible();

  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
  expect(dialog).toBe(false);
});
