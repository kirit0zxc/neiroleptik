/** Виземы VRM: пять форм рта, которыми покрывается вся речь. */
export type Viseme = 'aa' | 'ih' | 'ou' | 'ee' | 'oh';

export const VISEMES: Viseme[] = ['aa', 'ih', 'ou', 'ee', 'oh'];

export type VisemeWeights = Partial<Record<Viseme, number>>;

/** Один шаг раскладки: какая форма рта и сколько держится. */
export interface VisemeStep {
  viseme: Viseme | null;   // null — рот закрыт (согласная, пауза)
  weight: number;          // 0..1
  durationMs: number;
  /** Индекс символа в исходном тексте — по нему синхронизируемся с голосом. */
  charIndex: number;
}

/** Акцент тела и бровей во время речи. */
export interface SpeechAccent {
  atCharIndex: number;
  kind: 'nod' | 'question' | 'pause' | 'emphasis';
}

/** Что умеет источник голоса. Реализации подменяются без правок лица. */
export interface TTSProvider {
  readonly name: string;
  /** Поддерживает ли провайдер события границ слов. */
  readonly hasBoundaries: boolean;
  speak(text: string, handlers: TTSHandlers): Promise<void>;
  cancel(): void;
}

export interface TTSHandlers {
  /** Речь пошла. */
  onStart?: () => void;
  /** Дошли до слова, начинающегося с charIndex. Нужен для синхронизации. */
  onBoundary?: (charIndex: number) => void;
  /** Если провайдер отдаёт звук — сюда приходит узел для анализа. */
  onAudio?: (node: AudioNode, ctx: AudioContext) => void;
  onEnd?: () => void;
  onError?: (e: unknown) => void;
}
