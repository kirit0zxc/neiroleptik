import * as THREE from 'three';
import { VRMLoaderPlugin, VRMUtils, type VRM } from '@pixiv/three-vrm';
import { CLIPS, PRELOAD, resolveClip, type ClipMask, type ClipSpec } from '../../core/clips';
import type { FaceKey, GlowSpec, MascotRenderer, VrmExpression } from '../../core/types';
import { faceKeyAt, sortKeys } from '../../core/faceTrack';
import { forgetClip, loadClip } from './loadClip';
import { trimClip } from './trim';
import { createGLTFLoader } from './gltf';
import { baseOf, fetchBuffer, resolveAsset } from '../../assets';
import { prefersReducedMotion } from '../../motion';
import { CAMERA_DEFAULTS, parseCamera, type CameraSettings } from './camera';
import cameraFile from './camera.json';
import { clipCameraKey, parseClipCameras, type ClipCameras } from './clipCameras';
import clipCamerasFile from './clipCameras.json';

/** Свой кадр для отдельных клипов (общий для всех рендереров, правится на лету). */
const CLIP_CAMERAS: ClipCameras = parseClipCameras(clipCamerasFile);

export interface VrmRendererOptions extends Partial<CameraSettings> {
  /**
   * Откуда грузить модель и клипы. Относительные пути из реестра
   * клипов и modelUrl считаются от этой папки.
   *   '/static/mascot/'                       — приложение не в корне
   *   chrome.runtime.getURL('mascot/')        — расширение
   *   'https://cdn.example.com/yumi/'         — CDN
   */
  assetsBase?: string;
  /** Путь к модели; относительный — от assetsBase. */
  modelUrl?: string;
  /** Насколько охотно голова поворачивается за курсором, 0..1. */
  headFollow?: number;
  /**
   * Развести плечи наружу, в градусах.
   *
   * Анимации Mixamo рассчитаны на обычный торс, а худи у VRoid
   * заметно шире — без этого руки в покое тонут в одежде.
   */
  armSpread?: number;
  // Кадр (fit, zoom, offsetX/Y, yaw, pitch, fov, margin) — см. CameraSettings.
  background?: boolean;
  /**
   * Меньше фонового движения: без покачивания корпуса и пульсации
   * свечения. Клипы состояний играют как обычно — они несут смысл.
   * 'auto' — по системной настройке prefers-reduced-motion.
   */
  reducedMotion?: boolean | 'auto';
}

const DEFAULTS: Required<VrmRendererOptions> = {
  assetsBase: '/mascot/',
  modelUrl: 'model/yumi.vrm',
  headFollow: 0.35,
  armSpread: 8,
  // Кадр по умолчанию: встроенный, поверх — сохранённый в песочнице.
  ...CAMERA_DEFAULTS,
  ...parseCamera(cameraFile),
  background: false,
  reducedMotion: 'auto',
};

type ClipRange = readonly [number, number];

/**
 * Ключ действия микшера: один клип с другой маской или обрезкой — это
 * другие дорожки, а значит, отдельное действие. Суффикс разбираем строго
 * с конца: сам путь к файлу может содержать # и @.
 */
const variantKey = (clip: string, mask?: ClipMask, range?: ClipRange) =>
  clip + (mask ? `#${mask}` : '') + (range ? `@${range[0]}-${range[1]}` : '');
const VARIANT_SUFFIX = /(?:#(?:none|inplace|legs|strict))?(?:@[\d.]+-[\d.]+)?$/;
export const clipOf = (key: string) => key.replace(VARIANT_SUFFIX, '');

/**
 * Буферы моделей, общие для всех рендереров. Разбирать модель всё равно
 * приходится заново (её сцена принадлежит одному рендереру), но качать —
 * нет. Неудачная загрузка не кешируется.
 */
const models = new Map<string, Promise<ArrayBuffer>>();
function fetchModel(url: string) {
  let job = models.get(url);
  if (!job) {
    job = fetchBuffer(url);
    job.catch(() => models.delete(url));
    models.set(url, job);
  }
  return job;
}

export class VrmRenderer implements MascotRenderer {
  private opts: Required<VrmRendererOptions>;
  private scene = new THREE.Scene();
  private camera!: THREE.PerspectiveCamera;
  private gl!: THREE.WebGLRenderer;
  /**
   * Время прошлого кадра. Свой счётчик, а не THREE.Timer: Timer появился
   * в ядре three недавно, а пакет совместим и с более старыми версиями.
   */
  private lastTick = 0;
  /** Контейнер не виден (свёрнут, прокручен, display: none) — не рисуем. */
  private offscreen = false;
  private visibility: IntersectionObserver | null = null;
  /** Контекст WebGL потерян — не рисуем, пока браузер его не вернёт. */
  private contextLost = false;
  private vrm: VRM | null = null;
  private mixer: THREE.AnimationMixer | null = null;
  private container: HTMLElement | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private frame = 0;
  private elapsed = 0;

  // Клипы грузятся лениво и кешируются.
  private actions = new Map<string, THREE.AnimationAction>();
  private pending = new Map<string, Promise<void>>();
  private current: THREE.AnimationAction | null = null;
  private currentName = '';
  /** Для обрезанных клипов: где отрезок в исходном клипе и его полная длина. */
  private clipMeta = new Map<string, { start: number; end: number; full: number }>();
  /** Последний запрошенный клип — только он имеет право включиться. */
  private wantedName = '';
  /** Номер последнего запроса клипа: переключает только он. */
  private requestSeq = 0;
  /** Зацикленный клип, к которому возвращаемся после разового жеста. */
  private baseName = '';
  private disposed = false;

  // Мимика: держим целевые веса и подтягиваемся к ним плавно.
  private expressionTarget: Partial<Record<string, number>> = {};
  /** Основное выражение состояния — действует вне дорожки. */
  private baseExpression: { expression: VrmExpression; weight: number } = { expression: 'neutral', weight: 0 };
  private faceTrack: { clip: string; keys: FaceKey[] } | null = null;
  /** Какая метка сейчас применена: -1 — основное выражение, -2 — пересчитать. */
  private faceIndex = -2;
  private expressionNow: Partial<Record<string, number>> = {};

  // Взгляд.
  private gazeTarget = new THREE.Vector2(0, 0);
  private gazeNow = new THREE.Vector2(0, 0);
  private lookAtAnchor = new THREE.Object3D();
  /**
   * Габариты модели в позе покоя, замеренные один раз при загрузке.
   * Если мерить по текущей позе, кадр дрожал бы вместе с анимацией.
   */
  private bounds: {
    min: THREE.Vector3; max: THREE.Vector3;
    width: number; hips: number | null; chest: number | null; head: THREE.Vector3 | null;
  } | null = null;
  /** Точка взгляда «прямо в камеру» и оси камеры — от них считается взгляд. */
  private gazeBase = new THREE.Vector3();
  private camRight = new THREE.Vector3(1, 0, 0);
  private camUp = new THREE.Vector3(0, 1, 0);
  private gazePoint = new THREE.Vector3();
  /** Куда сейчас смотрит камера — её lookAt. */
  private camTarget = new THREE.Vector3();
  /** Цель плавного переезда камеры (смена клипа со своим кадром); null — стоим. */
  private camGoal: { pos: THREE.Vector3; target: THREE.Vector3; fov: number; gaze: THREE.Vector3 } | null = null;
  /** Каким набором настроек камера расставлена — чтобы не переезжать без нужды. */
  private camKey = '';
  private tmpVec = new THREE.Vector3();

  private blinkValue = 0;
  private blinkUntil = 0;
  private blinkDuration = 110;
  /** Целевые и текущие веса пяти визем. */
  private visemeTarget: Record<string, number> = {};
  private visemeNow: Record<string, number> = {};

  private speaking = false;
  /** Короткие акценты речи: кивки, брови, паузы. */
  private accents: { kind: string; t0: number; dur: number }[] = [];
  /** Мгновенная подмешка выражения поверх базовой эмоции. */
  private accentExpr: { name: string; weight: number; t0: number; dur: number } | null = null;

  private armSpread = 0;
  /** Окно, в котором живёт контейнер, — от него кадры и размеры. */
  private win: Window = window;
  private reducedMotion = false;

  /**
   * Позы костей до того, как мы добавили процедурную поправку.
   *
   * Микшер перезаписывает только те кости, для которых в текущем клипе
   * есть дорожка. Для остальных наш `+=` копился бы кадр за кадром и
   * раскручивал кость — поэтому каждый кадр сначала откатываем добавку.
   */
  private offsetBases = new Map<THREE.Object3D, THREE.Quaternion>();
  private glow: GlowSpec = { color: '#9ED8BC', intensity: 0.25, pulseHz: 0 };
  private glowColor = new THREE.Color('#9ED8BC');
  private rimMaterials: any[] = [];

  constructor(options: VrmRendererOptions = {}) {
    this.opts = { ...DEFAULTS, ...options };
    this.armSpread = this.opts.armSpread;
    this.reducedMotion = prefersReducedMotion(this.opts.reducedMotion);
  }

  /** Подобрать разведение плеч на лету, в градусах. */
  setArmSpread(deg: number) { this.armSpread = deg; }
  getArmSpread() { return this.armSpread; }

  // ─── Инициализация ────────────────────────────────────────

  async init(container: HTMLElement) {
    this.container = container;
    // Юми может жить в другом окне (Picture-in-Picture). Кадры, размеры и
    // плотность пикселей берём у него: окно главной вкладки, когда та
    // скрыта, перестаёт рисовать — и Юми в отдельном окне замерла бы.
    this.win = container.ownerDocument.defaultView ?? window;

    this.gl = new THREE.WebGLRenderer({ alpha: !this.opts.background, antialias: true });
    this.gl.setPixelRatio(Math.min(this.win.devicePixelRatio, 2));
    this.gl.outputColorSpace = THREE.SRGBColorSpace;
    if (!this.opts.background) this.gl.setClearAlpha(0);
    container.appendChild(this.gl.domElement);
    // Картинка декоративная: всё, что Юми сообщает, дублируется
    // интерфейсом приложения, а скринридеру canvas ничего не скажет.
    this.gl.domElement.setAttribute('aria-hidden', 'true');
    Object.assign(this.gl.domElement.style, {
      width: '100%', height: '100%', display: 'block',
    });

    this.camera = new THREE.PerspectiveCamera(this.opts.fov, 1, 0.1, 50);
    this.scene.add(this.lookAtAnchor);
    this.setupLights();
    this.resize();

    const Observer = (this.win as Window & typeof globalThis).ResizeObserver ?? ResizeObserver;
    this.resizeObserver = new Observer(() => this.resize());
    this.resizeObserver.observe(container);

    // Браузер отбирает контекст, когда на странице слишком много WebGL
    // или GPU сбросился. three восстанавливает ресурсы сам — нам
    // остаётся остановить цикл и запустить его снова.
    const canvas = this.gl.domElement;
    canvas.addEventListener('webglcontextlost', this.onContextLost);
    canvas.addEventListener('webglcontextrestored', this.onContextRestored);

    await this.loadModel();
    // Пока грузилась модель, рендерер могли уже убрать со страницы.
    if (this.disposed) return;
    this.watchVisibility(container);
    // Цикл мог уже запустить onContextRestored — второй не нужен.
    if (!this.frame) this.loop();
  }

  /**
   * Не рисовать, пока контейнер не виден: Юми прокручена за экран,
   * окно свёрнуто или спрятано. Иначе она жгла бы 60 кадров в секунду
   * впустую — на ноутбуке это заметно по батарее.
   */
  private watchVisibility(container: HTMLElement) {
    const Observer = (this.win as Window & typeof globalThis).IntersectionObserver;
    if (!Observer) return;
    this.visibility = new Observer((entries) => {
      const visible = entries.some((e) => e.isIntersecting);
      if (visible === !this.offscreen) return;
      this.offscreen = !visible;
      if (visible && !this.frame) {
        // Первый кадр после паузы не должен получить дельту за всё время простоя.
        this.lastTick = 0;
        this.loop();
      }
    });
    this.visibility.observe(container);
  }

  private setupLights() {
    // MToon сам рисует тун-затенение, поэтому свет здесь мягкий:
    // он только задаёт настроение, а объём делает шейдер.
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1, 1.6, 2.2);
    this.scene.add(key);

    const fill = new THREE.DirectionalLight(0xc9d8ff, 0.5);
    fill.position.set(-1.6, 0.6, 1.0);
    this.scene.add(fill);

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x6a7a99, 0.7));
  }

  private async loadModel() {
    const url = resolveAsset(this.opts.modelUrl, this.opts.assetsBase);
    const loader = createGLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.parseAsync(await fetchModel(url), baseOf(url));
    const vrm: VRM = gltf.userData.vrm;
    if (this.disposed) { VRMUtils.deepDispose(gltf.scene); return; }

    // Склейка скелетов и выброс невидимой геометрии — заметно
    // снижает число draw call'ов на слабых машинах.
    VRMUtils.combineSkeletons(gltf.scene);
    try { VRMUtils.removeUnnecessaryVertices(gltf.scene); } catch { /* не критично */ }

    // Модели VRM 0.x стоят спиной к камере — разворачиваем.
    if (vrm.meta?.metaVersion === '0') VRMUtils.rotateVRM0(vrm);

    vrm.scene.traverse((obj) => { obj.frustumCulled = false; });
    this.scene.add(vrm.scene);
    this.vrm = vrm;
    this.mixer = new THREE.AnimationMixer(vrm.scene);
    this.mixer.addEventListener('finished', this.onClipFinished);

    if (vrm.lookAt) vrm.lookAt.target = this.lookAtAnchor;

    this.collectRimMaterials();
    this.measure();
    this.frameCamera();
    this.applyGlow();

    // Фоновая подгрузка самого частого — чтобы первое переключение
    // состояния не упиралось в сеть.
    for (const name of PRELOAD) void this.ensureClip(name);
  }

  /** Находим MToon-материалы: у них есть встроенный параметрический rim. */
  private collectRimMaterials() {
    this.rimMaterials = [];
    this.vrm?.scene.traverse((obj: any) => {
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      for (const m of mats) {
        if (m && 'parametricRimColorFactor' in m) this.rimMaterials.push(m);
      }
    });
  }

  /**
   * Вписываем модель в кадр по её реальным габаритам.
   *
   * Так не нужно подбирать расстояние вручную: поменяется причёска,
   * обувь или пропорции — камера пересчитает сама.
   */
  /** Замерить габариты модели — один раз, в позе покоя. */
  private measure() {
    const vrm = this.vrm;
    if (!vrm) return;
    // Без пересчёта матриц габариты окажутся от нулевой позы.
    vrm.scene.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(vrm.scene);
    if (box.isEmpty()) return;

    const humanoid = vrm.humanoid;
    const bone = (name: string) => {
      const node = humanoid?.getNormalizedBoneNode(name as never);
      return node ? node.getWorldPosition(new THREE.Vector3()) : null;
    };
    // Ширина кадра — по плечам, а не по габаритам: модель грузится
    // в T-позе, и размах рук втрое шире фигуры с опущенными руками.
    // Иначе в узком окне «по пояс» и «портрет» не приближали бы камеру.
    const l = bone('leftUpperArm');
    const r = bone('rightUpperArm');
    const shoulders = l && r ? Math.abs(l.x - r.x) : 0;
    const width = shoulders
      ? Math.min(box.max.x - box.min.x, shoulders * 3)
      : box.max.x - box.min.x;

    this.bounds = {
      min: box.min.clone(),
      max: box.max.clone(),
      width,
      hips: bone('hips')?.y ?? null,
      chest: (bone('chest') ?? bone('spine'))?.y ?? null,
      head: bone('head'),
    };
  }

  /**
   * Вписываем модель в кадр по её реальным габаритам.
   *
   * Так не нужно подбирать расстояние вручную: поменяется причёска,
   * обувь или пропорции — камера пересчитает сама. Поверх — настройки
   * кадра: крупность, сдвиг, поворот и наклон вокруг Юми.
   */
  private frameCamera(snap = true) {
    const b = this.bounds;
    if (!b) return;
    const o = this.getEffectiveCamera();
    this.camKey = JSON.stringify(o);

    let bottom = b.min.y;
    if (o.fit === 'half') bottom = b.hips ?? bottom;
    if (o.fit === 'portrait') bottom = b.chest ?? bottom;

    const top = b.max.y;
    const pad = 1 + o.margin * 2;
    const height = Math.max(top - bottom, 0.1) * pad;
    const width = Math.max(b.width, 0.1) * pad;

    const vFov = (o.fov * Math.PI) / 180;
    const aspect = this.camera.aspect || 1;

    // Берём большее из двух расстояний, иначе в узком окне
    // модель упрётся в боковые края.
    const byHeight = height / 2 / Math.tan(vFov / 2);
    const byWidth = width / 2 / (Math.tan(vFov / 2) * aspect);
    const distance = Math.max(byHeight, byWidth) / Math.max(o.zoom, 0.01);

    // Сдвиг Юми вправо и вверх — это камера, смотрящая левее и ниже.
    const target = new THREE.Vector3(-o.offsetX, (top + bottom) / 2 - o.offsetY, 0);
    const yaw = THREE.MathUtils.degToRad(o.yaw);
    const pitch = THREE.MathUtils.degToRad(o.pitch);
    const dir = new THREE.Vector3(
      Math.sin(yaw) * Math.cos(pitch),
      Math.sin(pitch),
      Math.cos(yaw) * Math.cos(pitch),
    );
    const pos = target.clone().addScaledVector(dir, distance);
    // «Прямо в камеру» — точка на линии от головы к камере.
    const gaze = (b.head ?? target).clone().addScaledVector(dir, distance);

    if (!snap) {
      // Переезд к кадру клипа — плавно, в цикле отрисовки.
      this.camGoal = { pos, target, fov: o.fov, gaze };
      return;
    }
    this.camGoal = null;
    this.camera.position.copy(pos);
    this.camTarget.copy(target);
    this.camera.fov = o.fov;
    this.gazeBase.copy(gaze);
    this.lookAtAnchor.position.copy(gaze);
    this.applyCameraPose();
  }

  /** Применить положение, взгляд и угол обзора камеры. */
  private applyCameraPose() {
    this.camera.lookAt(this.camTarget);
    this.camera.updateProjectionMatrix();
    this.camera.updateMatrixWorld();
    this.camRight.setFromMatrixColumn(this.camera.matrixWorld, 0);
    this.camUp.setFromMatrixColumn(this.camera.matrixWorld, 1);
  }

  /** Шаг плавного переезда камеры к кадру нового клипа. */
  private moveCamera(delta: number) {
    const g = this.camGoal;
    if (!g) return;
    const k = 1 - Math.exp(-delta * 4);
    this.camera.position.lerp(g.pos, k);
    this.camTarget.lerp(g.target, k);
    this.gazeBase.lerp(g.gaze, k);
    this.camera.fov += (g.fov - this.camera.fov) * k;
    if (this.camera.position.distanceTo(g.pos) < 1e-3 && Math.abs(g.fov - this.camera.fov) < 0.01) {
      this.camera.position.copy(g.pos);
      this.camTarget.copy(g.target);
      this.gazeBase.copy(g.gaze);
      this.camera.fov = g.fov;
      this.camGoal = null;
    }
    this.applyCameraPose();
  }

  /**
   * Сменился клип (или он остановлен) — переехать к его кадру, если тот
   * отличается от текущего. Правку кадров клипов из другого рендерера
   * на странице этот рендерер увидит при следующей смене клипа.
   */
  private followClipCamera() {
    if (!this.bounds || this.disposed) return;
    const key = JSON.stringify(this.getEffectiveCamera());
    // «Уменьшить движение» — кадр меняется сразу, без пролёта камеры.
    if (key !== this.camKey) this.frameCamera(this.reducedMotion);
  }

  // ─── Камера ───────────────────────────────────────────────

  /**
   * Для диагностики: сколько секунд анимации отрисовано и рисуется ли
   * Юми сейчас (не на паузе из-за невидимости или потери контекста).
   */
  stats(): { elapsed: number; drawing: boolean } {
    return { elapsed: this.elapsed, drawing: !this.offscreen && !this.contextLost && this.frame !== 0 };
  }

  /** Имя текущего клипа, как его знают состояния и файл кадров клипов. */
  private clipName(): string {
    const key = clipCameraKey(clipOf(this.currentName), this.opts.assetsBase);
    if (!this.currentName.startsWith('file:')) return key;
    // Файл из реестра, запущенный из списка, — под логическим именем,
    // на которое ссылаются состояния (Happy%20Idle.vrma → happyIdle).
    for (const [name, spec] of Object.entries(CLIPS)) if (spec.url === key) return name;
    return key;
  }

  /** Кадр с учётом своего кадра текущего клипа — то, что сейчас на экране. */
  getEffectiveCamera(): CameraSettings {
    const own = CLIP_CAMERAS[this.clipName()];
    return own ? { ...this.getCamera(), ...own } : this.getCamera();
  }

  /** Свой кадр клипа (только отличия от общего) или null. */
  getClipCamera(clip: string): Partial<CameraSettings> | null {
    return CLIP_CAMERAS[clip] ?? null;
  }

  /** Все клипы со своим кадром. */
  getClipCameras(): ClipCameras {
    return { ...CLIP_CAMERAS };
  }

  /**
   * Задать или убрать свой кадр клипа на лету. Если клип сейчас играет,
   * кадр применяется сразу — это нужно для настройки ползунками.
   */
  setClipCamera(clip: string, camera: Partial<CameraSettings> | null) {
    const parsed = camera ? parseCamera(camera) : {};
    if (Object.keys(parsed).length) CLIP_CAMERAS[clip] = parsed;
    else delete CLIP_CAMERAS[clip];
    if (this.clipName() === clip) this.frameCamera(true);
  }

  /** Текущие настройки общего кадра. */
  getCamera(): CameraSettings {
    const { fit, margin, zoom, offsetX, offsetY, yaw, pitch, fov } = this.opts;
    return { fit, margin, zoom, offsetX, offsetY, yaw, pitch, fov };
  }

  /** Поменять кадр на лету; значения зажимаются в допустимые пределы. */
  setCamera(patch: Partial<CameraSettings>) {
    Object.assign(this.opts, parseCamera(patch));
    this.frameCamera();
  }

  private resize() {
    if (!this.container) return;
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    this.gl.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    // Аспект участвует в подборе дистанции — пересобираем кадр.
    if (this.vrm) this.frameCamera();
  }

  // ─── Контракт рендерера ───────────────────────────────────

  setExpression(name: VrmExpression, weight: number) {
    this.baseExpression = { expression: name, weight };
    this.faceIndex = -2;
  }

  setFaceTrack(clip: string | null, keys: readonly FaceKey[] | null) {
    this.faceTrack = clip && keys?.length ? { clip, keys: sortKeys(keys) } : null;
    this.faceIndex = -2;
  }

  /** Целевое выражение; к нему лицо подтягивается плавно в applyExpressions. */
  private targetExpression(name: VrmExpression, weight: number) {
    // Гасим прошлые эмоции, оставляя служебные каналы в покое.
    for (const key of ['happy', 'angry', 'sad', 'relaxed', 'surprised']) {
      this.expressionTarget[key] = 0;
    }
    if (name !== 'neutral') this.expressionTarget[name] = weight;
  }

  /**
   * Лицо по дорожке: пока играет клип дорожки, берём метку по его
   * времени, иначе основное выражение. Цели меняем только при смене
   * метки — сглаживание в applyExpressions делает переход мягким.
   */
  private applyFaceTrack() {
    const track = this.faceTrack;
    const active = track && this.current && clipOf(this.currentName) === track.clip;
    // Метки стоят во времени исходного клипа — учитываем обрезку.
    const index = active ? faceKeyAt(track.keys, this.current!.time + this.offsetOf(this.currentName)) : -1;
    if (index === this.faceIndex) return;
    this.faceIndex = index;
    const face = index >= 0 ? track!.keys[index]! : this.baseExpression;
    this.targetExpression(face.expression, face.weight);
  }

  // ─── Управление клипом для студии ─────────────────────────

  /** Что играет сейчас: имя, время, длина, пауза. */
  private offsetOf(key: string) { return this.clipMeta.get(key)?.start ?? 0; }

  /**
   * Что играет сейчас. Время и длина — в шкале исходного клипа:
   * у обрезанного клипа start..end — играющий отрезок внутри duration.
   */
  getClipState(): {
    name: string; time: number; duration: number; start: number; end: number; paused: boolean;
  } | null {
    const a = this.current;
    if (!a) return null;
    const own = a.getClip().duration;
    const meta = this.clipMeta.get(this.currentName) ?? { start: 0, end: own, full: own };
    return {
      name: this.clipName(),
      time: a.time + meta.start,
      duration: meta.full,
      start: meta.start,
      end: meta.end,
      paused: a.paused,
    };
  }

  /** Остановить или продолжить текущий клип — поза и лицо замирают на месте. */
  setClipPaused(paused: boolean) {
    if (this.current) this.current.paused = paused;
  }

  /** Перемотать текущий клип; на паузе поза обновится в следующем кадре. */
  seekClip(time: number) {
    const a = this.current;
    if (!a) return;
    a.time = Math.min(Math.max(time - this.offsetOf(this.currentName), 0), a.getClip().duration);
    // Доигранный разовый клип снова оживает, если в него перемотали.
    if (!a.isRunning()) { a.enabled = true; a.play(); }
  }

  setGaze(x: number, y: number) { this.gazeTarget.set(x, y); }

  /** Совместимость: одиночное раскрытие рта мапим на «аа». */
  setMouth(openness: number) { this.setVisemes({ aa: openness }); }

  /** Полный набор визем — сюда пишет липсинк. */
  setVisemes(weights: Record<string, number>) {
    this.visemeTarget = weights;
  }

  /**
   * Речь идёт или нет.
   *
   * Пока Юми говорит, эмоция приглушается: happy и sad у VRoid
   * сами деформируют рот, и на полной силе они забивают виземы —
   * получается шевеление губ вместо артикуляции.
   */
  setSpeaking(active: boolean) { this.speaking = active; }

  /** Короткий акцент: кивок на точке, брови на вопросе, пауза. */
  accent(kind: 'nod' | 'question' | 'pause' | 'emphasis') {
    const now = performance.now();
    const dur = kind === 'pause' ? 320 : kind === 'emphasis' ? 460 : 380;
    this.accents.push({ kind, t0: now, dur });
    if (this.accents.length > 6) this.accents.shift();

    if (kind === 'question') {
      this.accentExpr = { name: 'surprised', weight: 0.3, t0: now, dur: 620 };
    } else if (kind === 'emphasis') {
      this.accentExpr = { name: 'happy', weight: 0.18, t0: now, dur: 480 };
    }
  }

  blink(durationMs = 110) {
    this.blinkDuration = durationMs;
    this.blinkUntil = performance.now() + durationMs;
  }

  setGlow(spec: GlowSpec) {
    this.glow = spec;
    this.glowColor.set(spec.color);
    this.applyGlow();
  }

  playClip(
    name: string,
    opts: { loop?: boolean; fadeMs?: number; mask?: ClipMask; range?: ClipRange; returnTo?: string } = {},
  ) {
    const spec = resolveClip(name);
    const mask = spec && opts.mask && spec.mask !== opts.mask ? opts.mask : undefined;
    const range = spec && opts.range ? opts.range : undefined;
    if (!spec || (!mask && !range)) { this.request(name, opts); return; }
    this.request(variantKey(name, mask, range), opts, mask ? { ...spec, mask } : spec, range);
  }

  /**
   * Запомнить, что хотим играть, и включить, когда клип загрузится.
   *
   * Клипы грузятся с разной скоростью: без этой проверки медленный
   * клип, запрошенный раньше, перебил бы тот, что попросили позже.
   * Сверяется номер запроса, а не только имя: при перезагрузке файла
   * два запроса одного клипа могут завершиться в обратном порядке, и
   * устаревший не должен перезапустить клип со своими loop и fade.
   */
  private request(
    name: string, opts: { loop?: boolean; fadeMs?: number; returnTo?: string }, spec?: ClipSpec, range?: ClipRange,
  ) {
    this.wantedName = name;
    if (opts.loop) this.baseName = name;
    else if (opts.returnTo) this.baseName = opts.returnTo;
    const ticket = ++this.requestSeq;
    void this.ensureClip(name, spec, range).then(() => {
      if (ticket === this.requestSeq && this.wantedName === name) this.switchTo(name, opts);
    });
  }

  /** Разовый жест доигран — возвращаемся в зацикленный фон, а не замираем. */
  onClipEnd: ((name: string) => void) | null = null;

  private onClipFinished = (e: { action: THREE.AnimationAction }) => {
    if (e.action !== this.current) return;
    const finished = this.currentName;
    if (this.baseName && this.baseName !== finished) {
      this.request(this.baseName, { loop: true, fadeMs: 400 });
    }
    this.onClipEnd?.(clipOf(finished));
  };

  /**
   * Проиграть файл напрямую, в обход реестра состояний.
   * Нужно для песочницы: бросила файл в папку — сразу проверила.
   */
  playFile(url: string, opts: { mask?: ClipMask; loop?: boolean; reload?: boolean } = {}) {
    const mask = opts.mask ?? 'inplace';
    const key = `file:${url}:${mask}`;
    // reload — перечитать файл с диска (его могли подменить). Камеру при
    // этом не трогаем: клип сейчас же заиграет снова, и возврат к общему
    // кадру с переездом обратно был бы лишним качанием.
    if (opts.reload) this.dropFile(url);
    this.request(key, { loop: opts.loop ?? false, fadeMs: 250 }, { url, mask });
  }

  /**
   * Сбросить разобранный файл, чтобы перечитать его с диска заново.
   * Если он сейчас играл — клипа больше нет, камера возвращается к
   * общему кадру.
   */
  forgetFile(url: string) {
    this.dropFile(url);
    this.followClipCamera();
  }

  private dropFile(url: string) {
    // Начатая загрузка того же файла устарела: она отдала бы прежнее
    // содержимое. Следующий запрос начнёт загрузку заново.
    // oxlint-disable-next-line unicorn/no-useless-spread -- копия: набор меняется внутри цикла
    for (const key of [...this.pending.keys()]) {
      if (key.startsWith(`file:${url}:`)) this.pending.delete(key);
    }
    forgetClip(resolveAsset(url, this.opts.assetsBase));
    // oxlint-disable-next-line unicorn/no-useless-spread -- копия: набор меняется внутри цикла
    for (const key of [...this.actions.keys()]) {
      if (key.startsWith(`file:${url}:`)) {
        const action = this.actions.get(key);
        if (action) {
          action.stop();
          this.mixer?.uncacheAction(action.getClip());
          this.mixer?.uncacheClip(action.getClip());
        }
        this.actions.delete(key);
        if (this.currentName === key) { this.current = null; this.currentName = ''; }
        if (this.baseName === key) this.baseName = '';
      }
    }
  }

  stopClip(fadeMs = 250) {
    this.current?.fadeOut(fadeMs / 1000);
    this.current = null;
    this.currentName = '';
    this.wantedName = '';
    this.baseName = '';
    // Клипа больше нет — и его кадра тоже.
    this.followClipCamera();
  }

  capabilities() {
    return {
      clips: [...this.actions.keys()],
      expressions: ['neutral', 'happy', 'angry', 'sad', 'relaxed', 'surprised'],
    };
  }

  private onContextLost = (e: Event) => {
    e.preventDefault();
    this.contextLost = true;
    this.win.cancelAnimationFrame(this.frame);
    this.frame = 0;
  };

  private onContextRestored = () => {
    if (this.disposed) return;
    this.contextLost = false;
    // Без сброса первый кадр получил бы дельту за всё время простоя.
    this.lastTick = 0;
    this.loop();
  };

  /**
   * Где на экране голова — в координатах окна.
   *
   * По ней ядро считает взгляд: Юми может сидеть в углу, и смотреть
   * она должна на курсор относительно себя, а не центра окна.
   */
  getHeadScreenPosition(): { x: number; y: number } | null {
    const head = this.vrm?.humanoid?.getNormalizedBoneNode('head');
    const container = this.container;
    if (!head || !container) return null;
    head.getWorldPosition(this.tmpVec).project(this.camera);
    const rect = container.getBoundingClientRect();
    return {
      x: rect.left + (this.tmpVec.x + 1) / 2 * rect.width,
      y: rect.top + (1 - this.tmpVec.y) / 2 * rect.height,
    };
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.win.cancelAnimationFrame(this.frame);
    this.gl?.domElement.removeEventListener('webglcontextlost', this.onContextLost);
    this.gl?.domElement.removeEventListener('webglcontextrestored', this.onContextRestored);
    this.visibility?.disconnect();
    this.visibility = null;
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    if (this.mixer) {
      this.mixer.removeEventListener('finished', this.onClipFinished);
      this.mixer.stopAllAction();
      if (this.vrm) this.mixer.uncacheRoot(this.vrm.scene);
    }
    if (this.vrm) VRMUtils.deepDispose(this.vrm.scene);
    // forceContextLoss отдаёт контекст браузеру сразу: их число
    // на страницу ограничено, а перемонтирование не должно его съедать.
    this.gl?.forceContextLoss();
    this.gl?.dispose();
    this.gl?.domElement.remove();
    this.actions.clear();
    this.clipMeta.clear();
    this.pending.clear();
    this.offsetBases.clear();
    this.rimMaterials = [];
    this.mixer = null;
    this.vrm = null;
    this.container = null;
  }

  // ─── Клипы ────────────────────────────────────────────────

  private ensureClip(name: string, override?: ClipSpec, range?: ClipRange): Promise<void> {
    if (this.actions.has(name)) return Promise.resolve();
    const existing = this.pending.get(name);
    if (existing) return existing;

    const spec = override ?? resolveClip(name);
    if (!spec || !this.vrm || !this.mixer) return Promise.resolve();

    // Загрузка «своя», пока она записана в pending: dropFile вычёркивает
    // её, и тогда догрузившаяся старая версия файла не попадёт в микшер.
    const isLatest = () => this.pending.get(name) === job;
    const job: Promise<void> = loadClip({ ...spec, url: resolveAsset(spec.url, this.opts.assetsBase) }, this.vrm)
      .then((clip) => {
        if (!isLatest() || !clip || !this.mixer || this.disposed) return;
        const full = clip.duration;
        let played = clip;
        // Обрезка, начинающаяся за концом клипа, дала бы клип в один кадр:
        // разовый тут же «доиграл» бы, а петля дёргалась. Играем целиком.
        const usable = range && range[0] < full - 0.05 ? range : undefined;
        if (range && !usable) {
          console.warn(`[mascot] обрезка ${range[0]}–${range[1]} с за пределами клипа «${name}» (${full.toFixed(2)} с) — играю целиком`);
        }
        if (usable) {
          played = trimClip(clip, usable[0], usable[1]);
          this.evictVariants(name);
        }
        const start = usable ? Math.max(usable[0], 0) : 0;
        this.clipMeta.set(name, { start, end: start + played.duration, full });
        this.actions.set(name, this.mixer.clipAction(played));
      })
      .catch((err) => {
        // Клип не загрузился — маскот просто не сделает этот жест.
        console.warn(`[mascot] клип "${name}" не загрузился`, err);
      })
      .finally(() => {
        if (!isLatest()) return;
        this.pending.delete(name);
        // Ждали именно его, он так и не появился (ошибка загрузки или в
        // файле нет анимации), а прежний клип уже сброшен (перезагрузка
        // файла) — камере не на чем держать кадр клипа.
        if (this.wantedName === name && !this.current && !this.actions.has(name)) this.followClipCamera();
      });

    this.pending.set(name, job);
    return job;
  }

  /**
   * Убрать прошлые обрезки того же клипа. Пока в студии двигают границы,
   * каждая правка — новое действие; без уборки они копились бы в микшере.
   */
  private evictVariants(keep: string) {
    // Отрезаем только суффикс обрезки с конца: в самом пути может быть @.
    const prefix = keep.replace(/@[\d.]+-[\d.]+$/, '') + '@';
    for (const [key, action] of this.actions) {
      if (!key.startsWith(prefix) || key === keep) continue;
      if (key === this.currentName || key === this.baseName || key === this.wantedName) continue;
      action.stop();
      this.mixer?.uncacheAction(action.getClip());
      this.mixer?.uncacheClip(action.getClip());
      this.actions.delete(key);
      this.clipMeta.delete(key);
    }
  }

  private switchTo(name: string, opts: { loop?: boolean; fadeMs?: number }) {
    const action = this.actions.get(name);
    if (!action) return;
    const fade = (opts.fadeMs ?? 280) / 1000;

    if (this.currentName === name) {
      // Зацикленный уже идёт — не дёргаем. Разовый жест просили
      // повторить (второй success подряд) — проигрываем заново.
      if (opts.loop) {
        // Тот же клип, но раньше он шёл разово — переводим в повтор.
        if (action.loop !== THREE.LoopRepeat) {
          action.setLoop(THREE.LoopRepeat, Infinity);
          action.clampWhenFinished = false;
          if (!action.isRunning()) action.reset().play();
        }
        return;
      }
      // Тот же клип мог идти по кругу (студия, прошлое состояние) —
      // разовый запрос должен доиграть один раз и остановиться.
      action.setLoop(THREE.LoopOnce, Infinity);
      action.clampWhenFinished = true;
      action.reset().play();
      return;
    }

    action.reset();
    action.setLoop(opts.loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
    action.clampWhenFinished = !opts.loop;
    action.enabled = true;
    action.setEffectiveWeight(1);
    action.fadeIn(fade).play();

    this.current?.fadeOut(fade);
    this.current = action;
    this.currentName = name;
    this.followClipCamera();
  }

  // ─── Цикл отрисовки ───────────────────────────────────────

  private loop = () => {
    if (this.disposed || this.contextLost || this.offscreen) { this.frame = 0; return; }
    this.frame = this.win.requestAnimationFrame(this.loop);
    const now = performance.now();
    const delta = this.lastTick ? Math.min((now - this.lastTick) / 1000, 0.1) : 0;
    this.lastTick = now;
    this.elapsed += delta;

    // Порядок важен: сначала клип пишет в кости, потом мы кладём
    // поверх процедурку, и только затем vrm.update переносит всё
    // на реальный скелет и считает физику волос.
    // Сначала снимаем вчерашние добавки, потом микшер кладёт позу клипа,
    // и только после этого накладываем процедурку заново.
    this.restoreOffsets();
    this.mixer?.update(delta);
    this.moveCamera(delta);
    this.applyFaceTrack();
    this.applyArmSpread();
    this.applyBreathing();
    this.applyHeadFollow(delta);
    this.applyAccents();
    this.applyExpressions(delta);
    this.pulseGlow();
    this.vrm?.update(delta);

    this.gl.render(this.scene, this.camera);
  };

  /** Вернуть кости в состояние, в котором их оставил клип. */
  private restoreOffsets() {
    for (const [node, q] of this.offsetBases) node.quaternion.copy(q);
    this.offsetBases.clear();
  }

  /** Добавить поворот к кости, запомнив исходное значение. */
  private addRotation(
    node: THREE.Object3D | null | undefined,
    axis: 'x' | 'y' | 'z',
    radians: number,
  ) {
    if (!node || !radians) return;
    if (!this.offsetBases.has(node)) {
      this.offsetBases.set(node, node.quaternion.clone());
    }
    node.rotation[axis] += radians;
  }

  /**
   * Отводим плечи наружу поверх клипа.
   *
   * Правка аддитивная: клип продолжает управлять рукой как задумано,
   * мы лишь сдвигаем её по дуге настолько, чтобы она вышла из ткани.
   * Руки в T-позе смотрят вдоль X, так что поворот вокруг Z
   * поднимает их — для правой руки знак зеркальный.
   */
  private applyArmSpread() {
    if (!this.armSpread) return;
    const humanoid = this.vrm?.humanoid;
    if (!humanoid) return;

    const rad = (this.armSpread * Math.PI) / 180;
    this.addRotation(humanoid.getNormalizedBoneNode('leftUpperArm'), 'z', rad);
    this.addRotation(humanoid.getNormalizedBoneNode('rightUpperArm'), 'z', -rad);
  }

  /** Дыхание и лёгкое покачивание — то, чего не дают клипы. */
  private applyBreathing() {
    const humanoid = this.vrm?.humanoid;
    if (!humanoid || this.reducedMotion) return;

    const chest = humanoid.getNormalizedBoneNode('chest')
      ?? humanoid.getNormalizedBoneNode('spine');
    this.addRotation(chest, 'x', Math.sin(this.elapsed * 1.55) * 0.012);

    // Период другой, чтобы покачивание не совпадало с дыханием —
    // совпадение читается как механика.
    const spine = humanoid.getNormalizedBoneNode('spine');
    this.addRotation(spine, 'y', Math.sin(this.elapsed * 0.42) * 0.015);
    this.addRotation(spine, 'z', Math.cos(this.elapsed * 0.31) * 0.008);
  }

  /** Голова мягко доворачивается за курсором поверх клипа. */
  private applyHeadFollow(delta: number) {
    const k = 1 - Math.exp(-delta * 6);
    this.gazeNow.lerp(this.gazeTarget, k);
    // Взгляд считается в осях камеры — так он верен при любом ракурсе.
    this.gazePoint.copy(this.gazeBase)
      .addScaledVector(this.camRight, this.gazeNow.x * 1.2)
      .addScaledVector(this.camUp, this.gazeNow.y * 0.6);
    this.lookAtAnchor.position.lerp(this.gazePoint, k);

    const neck = this.vrm?.humanoid?.getNormalizedBoneNode('neck');
    const w = this.opts.headFollow;
    this.addRotation(neck, 'y', this.gazeNow.x * 0.42 * w);
    this.addRotation(neck, 'x', -this.gazeNow.y * 0.26 * w);
  }

  /**
   * Кивки и наклоны на знаках препинания.
   *
   * Говорящая голова, которая не двигается, выглядит мёртвой,
   * а равномерное покачивание — механическим. Привязка к пунктуации
   * даёт движение там, где его ждёт слушатель.
   */
  private applyAccents() {
    if (!this.accents.length) return;
    const neck = this.vrm?.humanoid?.getNormalizedBoneNode('neck');
    const head = this.vrm?.humanoid?.getNormalizedBoneNode('head');
    const now = performance.now();

    this.accents = this.accents.filter((a) => now - a.t0 < a.dur);

    for (const a of this.accents) {
      const t = (now - a.t0) / a.dur;
      // Полволны синуса: вышло и вернулось, без рывка на концах.
      const curve = Math.sin(t * Math.PI);

      if (a.kind === 'nod') {
        this.addRotation(head ?? neck, 'x', curve * 0.09);
      } else if (a.kind === 'emphasis') {
        this.addRotation(head ?? neck, 'x', curve * 0.14);
      } else if (a.kind === 'question') {
        this.addRotation(head ?? neck, 'x', -curve * 0.06);
        this.addRotation(neck, 'z', curve * 0.04);
      } else {
        this.addRotation(neck, 'z', curve * 0.05);
      }
    }
  }

  private applyExpressions(delta: number) {
    const em = this.vrm?.expressionManager;
    if (!em) return;
    const k = 1 - Math.exp(-delta * 9);
    const now = performance.now();

    // Пока идёт речь, эмоция уступает место артикуляции.
    const emotionScale = this.speaking ? 0.55 : 1;

    // Мгновенная подмешка: брови на вопросе, улыбка на ударении.
    let accentName = '';
    let accentWeight = 0;
    if (this.accentExpr) {
      const t = (now - this.accentExpr.t0) / this.accentExpr.dur;
      if (t >= 1) {
        this.accentExpr = null;
      } else {
        accentName = this.accentExpr.name;
        accentWeight = this.accentExpr.weight * Math.sin(t * Math.PI);
      }
    }

    let strongestEmotion = 0;
    for (const key of ['happy', 'angry', 'sad', 'relaxed', 'surprised']) {
      let target = (this.expressionTarget[key] ?? 0) * emotionScale;
      if (key === accentName) target = Math.max(target, accentWeight);

      const now_ = this.expressionNow[key] ?? 0;
      const next = now_ + (target - now_) * k;
      this.expressionNow[key] = next;
      em.setValue(key, next);
      strongestEmotion = Math.max(strongestEmotion, next);
    }

    // Моргание: быстро вниз, чуть медленнее вверх — так естественнее.
    const left = this.blinkUntil - performance.now();
    const want = left > 0 ? Math.min(1, left / (this.blinkDuration * 0.45)) : 0;
    this.blinkValue += (want - this.blinkValue) * (want > this.blinkValue ? 0.6 : 0.25);
    // Сильная эмоция у VRoid сама прищуривает глаза; моргание поверх
    // читается как подёргивание, поэтому гасим его пропорционально.
    em.setValue('blink', this.blinkValue * (1 - strongestEmotion * 0.7));

    // Виземы: подтягиваемся быстро, иначе рот мажет мимо слогов,
    // и закрываемся чуть медленнее, чем открываемся.
    for (const v of ['aa', 'ih', 'ou', 'ee', 'oh']) {
      const target = this.visemeTarget[v] ?? 0;
      const cur = this.visemeNow[v] ?? 0;
      const speed = target > cur ? 26 : 17;
      const next = cur + (target - cur) * (1 - Math.exp(-delta * speed));
      this.visemeNow[v] = next;
      em.setValue(v, next);
    }
  }

  // ─── Свет по контуру ──────────────────────────────────────

  private applyGlow() { this.pulseGlow(true); }

  /**
   * Rim light берём не шейдерным хаком, а встроенным параметрическим
   * римом MToon — он штатный, анимируется и не ломается при обновлениях.
   */
  private pulseGlow(force = false) {
    if (!this.rimMaterials.length) return;
    const pulse = this.glow.pulseHz > 0 && !this.reducedMotion
      ? 0.62 + 0.38 * Math.sin(this.elapsed * Math.PI * 2 * this.glow.pulseHz)
      : 1;
    const intensity = this.glow.intensity * pulse;
    if (!force && Math.abs(intensity) < 1e-4) return;

    for (const m of this.rimMaterials) {
      m.parametricRimColorFactor?.copy(this.glowColor).multiplyScalar(intensity);
      m.parametricRimFresnelPowerFactor = 3.2;
      m.parametricRimLiftFactor = 0;
      m.rimLightingMixFactor = 1;
    }
  }
}
