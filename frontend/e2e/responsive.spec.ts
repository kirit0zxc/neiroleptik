import { expect, type Page, test } from '@playwright/test';

/** Узкий экран (specs/frontend.md, 6a): страница не шире окна, широкие таблицы прокручиваются внутри. */
test.use({ viewport: { width: 360, height: 800 } });

/** Ширина документа не больше окна; замер — после загрузки данных страницы. */
async function expectFits(page: Page, label: string) {
  await page.waitForLoadState('networkidle');
  // Строкой: в e2e нет типов DOM, код выполняется в браузере.
  const { doc, vw } = await page.evaluate<{ doc: number; vw: number }>('({ doc: document.documentElement.scrollWidth, vw: window.innerWidth })');
  expect(doc, label).toBeLessThanOrEqual(vw);
}

test('страницы не выходят за ширину телефона', async ({ page }) => {
  await page.goto('/');
  await expectFits(page, '/login');
  await page.getByLabel('Логин').fill('admin');
  await page.getByLabel('Пароль').fill('admin');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

  for (const path of ['/', '/fleet', '/profiles', '/profiles/new', '/events', '/alerts', '/audit', '/fleet/new', '/nope']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    // Таблицы с данными, а не пустые: иначе проверка проходила бы впустую.
    if (['/fleet', '/profiles', '/audit'].includes(path)) await expect(page.getByRole('row').nth(1)).toBeVisible();
    await expectFits(page, path);
  }

  await page.goto('/profiles');
  await page.getByRole('row').nth(1).getByRole('link').first().click();
  await expect(page).toHaveURL(/\/profiles\/[^/]+$/);
  await expectFits(page, '/profiles/:id');

  await page.goto('/events');
  await page.getByRole('link', { name: /^Запись сессии …/ }).first().click();
  await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}$/);
  await expectFits(page, '/sessions/:id');
});
