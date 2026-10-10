import type { SpeechAccent, Viseme, VisemeStep } from './types';

/**
 * Русские гласные по форме рта.
 *
 * Пяти визем VRM не хватает на фонетику один в один, поэтому
 * раскладываем по близости артикуляции: важно не фонетическое
 * соответствие, а чтобы рот двигался правдоподобно.
 */
const VOWELS: Record<string, Viseme> = {
  а: 'aa', я: 'aa',
  о: 'oh', ё: 'oh',
  у: 'ou', ю: 'ou',
  и: 'ih', ы: 'ih',
  е: 'ee', э: 'ee',
};

/** Согласные с заметной формой рта. Остальные просто приоткрывают. */
const CONSONANTS: Record<string, { viseme: Viseme | null; weight: number }> = {
  // Губы смыкаются полностью
  м: { viseme: null, weight: 0 },
  б: { viseme: null, weight: 0 },
  п: { viseme: null, weight: 0 },
  // Губы вытягиваются
  ш: { viseme: 'ou', weight: 0.45 },
  ж: { viseme: 'ou', weight: 0.45 },
  щ: { viseme: 'ou', weight: 0.4 },
  ч: { viseme: 'ou', weight: 0.4 },
  // Губно-зубные
  ф: { viseme: 'ih', weight: 0.3 },
  в: { viseme: 'ih', weight: 0.3 },
};

const DEFAULT_CONSONANT = { viseme: 'ih' as Viseme, weight: 0.22 };

/** Среднее время на символ при обычном темпе речи. */
const MS_PER_CHAR = 68;
const VOWEL_FACTOR = 1.35;    // гласные тянутся дольше согласных
const PAUSE_COMMA = 160;
const PAUSE_SENTENCE = 330;

export interface SpeechPlan {
  steps: VisemeStep[];
  accents: SpeechAccent[];
  totalMs: number;
}

/**
 * Превращает текст в раскладку рта и акцентов.
 *
 * Тайминг здесь оценочный: точных длительностей фонем браузер не даёт.
 * Зато каждый шаг помнит свой индекс в тексте, и когда синтезатор
 * сообщает границу слова, раскладка подтягивается к реальной речи —
 * так расхождение не накапливается до конца фразы.
 */
export function planSpeech(text: string, rate = 1): SpeechPlan {
  const steps: VisemeStep[] = [];
  const accents: SpeechAccent[] = [];
  const scale = 1 / Math.max(rate, 0.1);
  let total = 0;

  const push = (s: VisemeStep) => { steps.push(s); total += s.durationMs; };

  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!.toLowerCase();

    if (VOWELS[ch]) {
      push({
        viseme: VOWELS[ch], weight: 1,
        durationMs: MS_PER_CHAR * VOWEL_FACTOR * scale, charIndex: i,
      });
      continue;
    }

    if (/[а-яё]/.test(ch)) {
      const c = CONSONANTS[ch] ?? DEFAULT_CONSONANT;
      push({
        viseme: c.viseme, weight: c.weight,
        durationMs: MS_PER_CHAR * scale, charIndex: i,
      });
      continue;
    }

    // Латиница — грубо, но лучше чем молчать
    if (/[a-z]/.test(ch)) {
      const v: Viseme = 'aeiou'.includes(ch) ? 'aa' : 'ih';
      push({
        viseme: v, weight: 'aeiou'.includes(ch) ? 0.85 : 0.22,
        durationMs: MS_PER_CHAR * scale, charIndex: i,
      });
      continue;
    }

    // Знаки препинания: пауза плюс повод шевельнуться
    if (',;:'.includes(ch)) {
      push({ viseme: null, weight: 0, durationMs: PAUSE_COMMA * scale, charIndex: i });
      accents.push({ atCharIndex: i, kind: 'pause' });
      continue;
    }
    if ('.!?…'.includes(ch)) {
      push({ viseme: null, weight: 0, durationMs: PAUSE_SENTENCE * scale, charIndex: i });
      accents.push({ atCharIndex: i, kind: ch === '?' ? 'question' : 'nod' });
      continue;
    }
    if (ch === ' ') {
      push({ viseme: null, weight: 0, durationMs: MS_PER_CHAR * 0.6 * scale, charIndex: i });
    }
  }

  return { steps, accents, totalMs: total };
}
