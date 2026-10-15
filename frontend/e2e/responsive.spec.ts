import { expect, test } from '@playwright/test';

/** Узкий экран (specs/frontend.md, 6a): страница не шире окна, широкие таблицы прокручиваются внутри. */
test.use({ viewport: { width: 375, height: 812 } });

test('страницы не выходят за ширину телефона', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Логин').fill('admin');
  await page.getByLabel('Пароль').fill('admin');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  for (const path of ['/', '/fleet', '/profiles', '/profiles/new', '/events', '/alerts', '/audit', '/fleet/new']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(width, path).toBeLessThanOrEqual(375);
  }
});
