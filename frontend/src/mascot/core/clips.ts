/**
 * Какую часть тела слушает клип.
 *
 * По умолчанию 'inplace': анимация идёт на всё тело, но смещение
 * таза отбрасывается, иначе клип, снятый не «in place», уводит
 * Юми из кадра — со стороны это выглядит как сбой.
 * Маска пригодится, когда Юми переедет в угол интерфейса
 * и ноги окажутся за кадром.
 */
export type ClipMask =
  | 'none'     // как есть, вместе с корневым движением
  | 'inplace'  // всё тело, но Юми не сходит с места
  | 'legs'     // убрать ноги и стопы, таз оставить (корпус качается)
  | 'strict';  // убрать ноги и таз целиком — только верх тела

export interface ClipSpec {
  /**
   * Путь к .vrma. Относительный считается от assetsBase рендерера,
   * абсолютный (/..., http:, blob:, chrome-extension: ...) берётся как есть.
   */
  url: string;
  mask: ClipMask;
}

const vrma = (file: string, mask: ClipMask = 'inplace'): ClipSpec =>
  ({ url: 'anim/vrma/' + encodeURIComponent(file), mask });

/**
 * Реестр клипов. Ключ — логическое имя, на него ссылаются состояния.
 * Чтобы добавить новое действие, достаточно строки здесь.
 */
export const CLIPS: Record<string, ClipSpec> = {
  // Mixamo, сконвертированные в VRMA
  idle:       vrma('Idle.vrma'),
  happyIdle:  vrma('Happy Idle.vrma'),
  sad:        vrma('Sad Idle.vrma'),
  victory:    vrma('Victory Idle.vrma'),
  wave:       vrma('Waving.vrma'),
  thinking:   vrma('Thinking.vrma'),
  lookAround: vrma('Look Around.vrma'),
  talking:    vrma('Talking.vrma'),
  thankful:   vrma('Thankful.vrma'),
  dismiss:    vrma('Dismissing Gesture.vrma'),
  nod:        vrma('Head Nod Yes.vrma'),
  dwarfIdle:  vrma('Dwarf Idle.vrma'),
  reacting:   vrma('Reacting.vrma'),

  // Официальный пак VRoid
  showBody:   vrma('VRMA_01.vrma'),
  // Начинает с корточек и выпрыгивает к камере — корпус двигается как снят.
  greeting:   vrma('VRMA_02.vrma', 'none'),
  peace:      vrma('VRMA_03.vrma'),
  shoot:      vrma('VRMA_04.vrma'),
  spin:       vrma('VRMA_05.vrma'),
  modelPose:  vrma('VRMA_06.vrma'),
  squat:      vrma('VRMA_07.vrma'),

  // CC0-пак; имена по файлам
  motionPose: vrma('001_motion_pose.vrma'),
  airplane:   vrma('CC0animation3airplane05.vrma'),
  bored:      vrma('CC0animationhima01.vrma'),
  idleAlt:    vrma('CC0animationidle04.vrma'),
  twirl:      vrma('CC0animationkurukuru01.vrma'),
  other:      vrma('CC0animationother2.vrma'),
  rotate:     vrma('CC0animationrotate02.vrma'),
  rotateAlt:  vrma('CC0animationrotate7.vrma'),
  turnLeft:   vrma('CC0animationrotate_left1.vrma'),
  turnRight:  vrma('CC0animationrotate_right2.vrma'),
  smallWave:  vrma('CC0animationsmallwve.vrma'),
  stand:      vrma('CC0animationstand01.vrma'),
  unknown3:   vrma('CC0animationunknown3.vrma'),
  unknown4:   vrma('CC0animationunknown4.vrma'),
  unwave:     vrma('CC0animationunwave.vrma'),

  // Танцы
  danceKawaii:   vrma('Kawaii Kaiwai.vrma'),
  danceMugi:     vrma('うい麦畑でつかまえて.vrma'),
  danceSynth:    vrma('ハッピーシンセサイザ.vrma'),
  danceScream:   vrma('愛♡スクリ〜ム！.vrma'),
};

/** Танцы — для случайного выбора или отдельной кнопки. */
export const DANCES: readonly string[] = ['danceKawaii', 'danceMugi', 'danceSynth', 'danceScream'];

/**
 * Найти клип: по имени в реестре или напрямую по пути к .vrma.
 *
 * Путь работает для файлов, которых нет в реестре: бросили в папку —
 * и сразу можно поставить в состояние, не правя код.
 */
export function resolveClip(name: string): ClipSpec | null {
  const known = CLIPS[name];
  if (known) return known;
  return /\.vrma$/i.test(name) ? { url: name, mask: 'inplace' } : null;
}

/** Что грузим сразу при старте — остальное подтянется лениво. */
export const PRELOAD = ['idle', 'thinking', 'wave'];
