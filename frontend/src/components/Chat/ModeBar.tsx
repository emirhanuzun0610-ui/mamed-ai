import { Zap, Scale, Brain, Sparkles, Mic, Volume2, VolumeX } from 'lucide-react';
import { useAppStore } from '../../lib/store';
import type { MamedModeId } from '../../lib/api';

const MODE_BUTTONS: Array<{
  id: MamedModeId;
  label: string;
  shortLabel: string;
  color: string;
  hint: string;
  Icon: typeof Zap;
}> = [
  {
    id: 'fast',
    label: 'Hızlı Mod',
    shortLabel: 'Hızlı',
    color: '#10b981',
    hint: 'En düşük gecikme ile hızlı sohbet ve basit komutlar',
    Icon: Zap,
  },
  {
    id: 'standard',
    label: 'Dengeli Mod',
    shortLabel: 'Dengeli',
    color: '#0ea5e9',
    hint: 'Günlük görevler ve araçlarla dengeli hız/kalite',
    Icon: Scale,
  },
  {
    id: 'deep',
    label: 'Derin Mod',
    shortLabel: 'Derin',
    color: '#a855f7',
    hint: 'Karmaşık mühendislik, analiz ve sıkı doğrulama',
    Icon: Brain,
  },
];

interface ModeBarProps {
  wakeListening?: boolean;
}

export function ModeBar({ wakeListening }: ModeBarProps) {
  const mamedMode = useAppStore((s) => s.mamedMode);
  const mamedAutoRoute = useAppStore((s) => s.mamedAutoRoute);
  const wakeWordEnabled = useAppStore((s) => s.wakeWordEnabled);
  const voiceOutputEnabled = useAppStore((s) => s.settings.voiceOutputEnabled);
  const setMamedMode = useAppStore((s) => s.setMamedMode);
  const setMamedAutoRoute = useAppStore((s) => s.setMamedAutoRoute);
  const setWakeWordEnabled = useAppStore((s) => s.setWakeWordEnabled);
  const updateSettings = useAppStore((s) => s.updateSettings);

  const activeCfg = MODE_BUTTONS.find((m) => m.id === mamedMode) ?? MODE_BUTTONS[1];

  return (
    <div
      className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 shrink-0"
      style={{
        borderBottom: `2px solid color-mix(in srgb, ${activeCfg.color} 45%, var(--color-border))`,
        background: `linear-gradient(90deg, color-mix(in srgb, ${activeCfg.color} 9%, transparent), transparent 65%)`,
      }}
    >
      {/* Sol: Mamed kimliği + aktif mod rozeti */}
      <div className="flex items-center gap-2 min-w-0">
        <span
          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold tracking-tight"
          style={{
            background: `color-mix(in srgb, ${activeCfg.color} 18%, transparent)`,
            color: activeCfg.color,
            border: `1px solid color-mix(in srgb, ${activeCfg.color} 45%, transparent)`,
          }}
        >
          <span
            className="w-2 h-2 rounded-full"
            style={{ background: activeCfg.color, boxShadow: `0 0 8px ${activeCfg.color}` }}
          />
          Mamed · {activeCfg.label}
        </span>
      </div>

      {/* Orta: 3 Karmaşıklık Modu + Oto */}
      <div className="flex items-center gap-1 overflow-x-auto max-w-full">
        {MODE_BUTTONS.map(({ id, shortLabel, color, hint, Icon }) => {
          const active = mamedMode === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => void setMamedMode(id)}
              title={hint}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer shrink-0"
              style={{
                background: active ? `color-mix(in srgb, ${color} 18%, var(--color-bg-secondary))` : 'transparent',
                color: active ? color : 'var(--color-text-secondary)',
                border: `1px solid ${active ? color : 'var(--color-border)'}`,
              }}
            >
              <Icon size={13} />
              <span>{shortLabel}</span>
            </button>
          );
        })}

        <button
          type="button"
          onClick={() => void setMamedAutoRoute(!mamedAutoRoute)}
          title="Mesaj karmaşıklığına göre otomatik mod seçimi"
          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer shrink-0"
          style={{
            background: mamedAutoRoute ? 'var(--color-accent-subtle)' : 'transparent',
            color: mamedAutoRoute ? 'var(--color-accent)' : 'var(--color-text-tertiary)',
            border: `1px solid ${mamedAutoRoute ? 'var(--color-accent)' : 'var(--color-border)'}`,
          }}
        >
          <Sparkles size={13} />
          <span>Oto</span>
        </button>

        <button
          type="button"
          onClick={() => setWakeWordEnabled(!wakeWordEnabled)}
          title='"Mamed" veya "Mamed uyan" sesli uyandırma dinlemesi'
          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer shrink-0"
          style={{
            background: wakeWordEnabled ? 'color-mix(in srgb, #10b981 16%, transparent)' : 'transparent',
            color: wakeWordEnabled ? '#10b981' : 'var(--color-text-tertiary)',
            border: `1px solid ${wakeWordEnabled ? '#10b981' : 'var(--color-border)'}`,
          }}
        >
          <Mic size={13} className={wakeListening ? 'animate-pulse' : ''} />
          <span className="hidden sm:inline">Mamed Uyan</span>
        </button>

        <button
          type="button"
          onClick={() => updateSettings({ voiceOutputEnabled: !voiceOutputEnabled, voiceAutoplay: !voiceOutputEnabled })}
          title={voiceOutputEnabled ? 'Sesli Türkçe yanıt açık' : 'Sesli Türkçe yanıt kapalı'}
          className="inline-flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs transition-all cursor-pointer shrink-0"
          style={{
            background: voiceOutputEnabled ? 'var(--color-bg-secondary)' : 'transparent',
            color: voiceOutputEnabled ? 'var(--color-accent)' : 'var(--color-text-tertiary)',
            border: '1px solid var(--color-border)',
          }}
        >
          {voiceOutputEnabled ? <Volume2 size={13} /> : <VolumeX size={13} />}
        </button>
      </div>
    </div>
  );
}
