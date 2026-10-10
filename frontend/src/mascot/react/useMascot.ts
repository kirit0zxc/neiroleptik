import { useEffect, useState, useMemo } from 'react';
import { getMascot } from '../singleton';
import type { MascotCore } from '../core/MascotCore';
import type { MascotState } from '../core/types';

/**
 * Управление маскотом из компонента + текущее состояние для UI.
 * По умолчанию — общий экземпляр, см. getMascot().
 */
export function useMascot(coreOverride?: MascotCore) {
  const core = useMemo(() => coreOverride ?? getMascot(), [coreOverride]);
  const [state, setState] = useState<MascotState>(core.getState());
  // Компонент мог появиться, когда Юми уже загружена, — события ready
  // он тогда не услышит, поэтому начальное значение берём у ядра.
  const [ready, setReady] = useState(() => core.getRenderer() !== null);
  const [bubble, setBubble] = useState<string | null>(null);
  const [speaking, setSpeaking] = useState(false);
  const [muted, setMuted] = useState(() => core.isMuted);

  useEffect(() => {
    let hide: ReturnType<typeof setTimeout> | undefined;
    const off = core.on((e) => {
      if (e.type === 'state') setState(e.state);
      if (e.type === 'ready') setReady(true);
      if (e.type === 'detached') setReady(false);
      if (e.type === 'muted') setMuted(e.muted);
      if (e.type === 'say') {
        clearTimeout(hide);
        setBubble(e.text);
        // Страховка: если синтезатор не отзовётся (нет голоса, нет
        // разрешения, звук выключен), пузырь всё равно уйдёт сам.
        hide = setTimeout(() => setBubble(null), 2000 + e.text.length * 90);
      }
      if (e.type === 'speaking') {
        setSpeaking(e.active);
        if (!e.active) {
          clearTimeout(hide);
          // Небольшая задержка, чтобы фраза не исчезала ровно
          // в тот момент, когда Юми закрывает рот.
          hide = setTimeout(() => setBubble(null), 700);
        }
      }
    });
    return () => { clearTimeout(hide); off(); };
  }, [core]);

  // Стабильные ссылки: их кладут в зависимости useEffect, и новые
  // функции на каждом рендере перезапускали бы эффекты впустую.
  const actions = useMemo(() => ({
    set: (s: MascotState) => core.setState(s),
    say: (text: string) => core.say(text),
    hush: () => core.hush(),
    setMuted: (m: boolean) => core.setMuted(m),
    setVoiceOptions: (o: { voiceName?: string; rate?: number; pitch?: number }) =>
      core.setVoiceOptions(o),
    lookAt: (x: number, y: number) => core.lookAt(x, y),
  }), [core]);

  return { state, ready, bubble, speaking, muted, ...actions, core };
}
