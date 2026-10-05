/**
 * Ядро маскота без отрисовки: состояния, мост к пайплайну, голос.
 * Ни three.js, ни React здесь нет — их подключают отдельные входы:
 *   mascot/vrm    — 3D-рендерер (three.js + VRM)
 *   mascot/react  — <Mascot/> и useMascot()
 */

// ─── Ядро ────────────────────────────────────────────────────
export { MascotCore } from './core/MascotCore';
export type { MascotCoreOptions } from './core/MascotCore';
export { STATES, BUILTIN_STATES, PRESETS, defineState, removeState } from './core/states';
export { parsePresets, toStateSpec, EXPRESSIONS } from './core/presets';
export { faceKeyAt } from './core/faceTrack';
export { TRANSITIONS, CHAINS, transitionFade, defineTransition, removeTransition } from './core/transitionTable';
export { parseTransitionsFile, transitionKey } from './core/transitions';
export type { ChainStep, TransitionsFile } from './core/transitions';
export type { FaceKey } from './core/faceTrack';
export type { Presets } from './core/presets';
export { CLIPS, DANCES, resolveClip } from './core/clips';
export type { ClipMask, ClipSpec } from './core/clips';
export * from './core/types';
export { getMascot, resetMascot } from './singleton';

// ─── Голос ───────────────────────────────────────────────────
export { Voice } from './voice/Voice';
export { BrowserTTS, DEFAULT_PREFERRED_VOICES } from './voice/BrowserTTS';
export { ServerTTS } from './voice/ServerTTS';
export type { ServerTTSOptions } from './voice/ServerTTS';
export { AudioLipSync } from './voice/AudioLipSync';
export { planSpeech } from './voice/ruVisemes';
export type { TTSProvider, TTSHandlers, Viseme, VisemeWeights } from './voice/types';

// ─── Мост к пайплайну ────────────────────────────────────────
export { MascotBridge, DEFAULT_RULES, normalize, parseEvent } from './bridge';
export type {
  BridgeRules, BridgeOptions, ReactionSpec, ResolvedReaction,
  ReactionDetail, NormalizedPayload, WrapFetchOptions,
} from './bridge';

export { connect } from './connect';
export type { ConnectOptions } from './connect';
