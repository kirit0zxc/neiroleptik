import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Без globals Vitest не подключает автоочистку Testing Library — делаем сами,
// иначе разметка прошлого теста остаётся в DOM следующего.
afterEach(() => cleanup());
