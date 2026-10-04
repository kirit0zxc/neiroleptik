/**
 * Обёртка для React. three.js подгружается лениво, отдельным чанком,
 * когда <Mascot/> впервые появляется на странице.
 */
export { Mascot } from './react/Mascot';
export type { MascotProps } from './react/Mascot';
export { useMascot } from './react/useMascot';
export { MascotWindow } from './react/window/MascotWindow';
export type { MascotWindowProps } from './react/window/MascotWindow';
export type { MenuItem, MenuSection } from './react/window/MenuBar';
export { ensureWindowStyles } from './react/window/styles';
