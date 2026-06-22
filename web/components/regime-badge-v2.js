'use client';

// D.8.0 Regime Badge — régimen invertido + guard ratios sumados.
// FRESH (suma ratios <120): CRITICO=entrar ya, BUENO=esperar
// DISCHARGED (suma >=120):  símbolo agotado, lógica normal

const COLORS = {
  green:   '#4ade80',
  amber:   '#fbbf24',
  orange:  '#fb923c',
  red:     '#f87171',
  cyan:    '#22d3ee',
  purple:  '#c084fc',
  mute:    '#6b7280',
  textD:   '#9ca3af',
  bg2:     '#1f2937',
  border:  '#374151',
};

const FONT_MONO = "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace";

// FRESH mode — colores invertidos: CRITICO=urgente entrar, BUENO=esperando oportunidad
const STYLES_FRESH = {
  BUENO:    { bg: COLORS.green  + '1a', border: COLORS.green  + '44', text: COLORS.green,  dot: COLORS.green,  action: '+5m esperar' },
  MEDIOCRE: { bg: COLORS.amber  + '1a', border: COLORS.amber  + '44', text: COLORS.amber,  dot: COLORS.amber,  action: '+3m'         },
  DIFICIL:  { bg: '#ff9f431a',          border: '#ff9f4344',           text: COLORS.orange, dot: COLORS.orange, action: '+1m rápido'  },
  CRITICO:  { bg: COLORS.cyan   + '1a', border: COLORS.cyan   + '44', text: COLORS.cyan,   dot: COLORS.cyan,   action: 'ENTRAR YA'   },
};

// DISCHARGED mode — símbolo agotado, lógica normal (más espera = más seguro)
const STYLES_DISCHARGED = {
  BUENO:    { bg: '#1e3a4a',            border: COLORS.cyan   + '33', text: COLORS.cyan,   dot: COLORS.cyan,   action: '+0s normal'  },
  MEDIOCRE: { bg: '#2a2a1a',            border: COLORS.amber  + '33', text: COLORS.amber,  dot: COLORS.amber,  action: '+2m agotado' },
  DIFICIL:  { bg: '#2a1a1a',            border: COLORS.orange + '33', text: COLORS.orange, dot: COLORS.orange, action: '+4m agotado' },
  CRITICO:  { bg: COLORS.purple + '1a', border: COLORS.purple + '44', text: COLORS.purple, dot: COLORS.purple, action: '+6m agotado' },
};

export function RegimeBadgeV2({ regimeData }) {
  if (!regimeData?.regime) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: 7,
        padding: '5px 10px', borderRadius: 5,
        background: COLORS.bg2, border: `1px solid ${COLORS.border}`,
      }}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: COLORS.mute, display: 'inline-block' }} />
        <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: COLORS.mute }}>RÉGIMEN D.8.0 — sin datos</span>
      </div>
    );
  }

  const isDischarged = regimeData.is_discharged ?? false;
  const styles = isDischarged ? STYLES_DISCHARGED : STYLES_FRESH;
  const st = styles[regimeData.regime] || styles.MEDIOCRE;

  const wr5      = regimeData.wr_5 != null ? `WR5 ${regimeData.wr_5}%` : null;
  const pnl2h    = regimeData.pnl_2h != null ? `PnL ${regimeData.pnl_2h >= 0 ? '+' : ''}${Number(regimeData.pnl_2h).toFixed(2)}` : null;
  const loss     = regimeData.consecutive_losses != null ? `L${regimeData.consecutive_losses}` : null;
  const ts       = regimeData.timing_state ? regimeData.timing_state.replace(/_/g, ' ') : null;
  const sumR     = regimeData.sum_ratios_10m ?? 0;
  const thr      = regimeData.threshold ?? 120;
  const nAligned = regimeData.count_aligned_10m ?? 0;
  const silenceS = regimeData.current_silence_s ?? 0;
  const silMin   = Math.floor(silenceS / 60);
  const silSec   = Math.floor(silenceS % 60);

  const modeBadgeText  = isDischarged ? 'DESCARGADO' : 'FRESCO';
  const modeBadgeColor = isDischarged ? COLORS.purple : COLORS.cyan;
  const sumBarPct      = Math.min((sumR / thr) * 100, 100);
  const sumBarColor    = isDischarged ? COLORS.purple : COLORS.cyan;

  return (
    <div style={{
      padding: '6px 10px', borderRadius: 5,
      background: st.bg, border: `1px solid ${st.border}`,
      display: 'flex', flexDirection: 'column', gap: 3,
    }}>
      {/* Row 1: régimen + modo */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: st.dot, display: 'inline-block' }} />
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800, color: st.text, letterSpacing: '0.05em' }}>
            {regimeData.regime}
          </span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: st.text, opacity: 0.75 }}>
            · {st.action}
          </span>
        </div>
        <span style={{ fontFamily: FONT_MONO, fontSize: 8, fontWeight: 700, color: modeBadgeColor, letterSpacing: '0.08em' }}>
          {modeBadgeText}
        </span>
      </div>

      {/* Row 2: barra de ratios sumados */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: COLORS.textD, whiteSpace: 'nowrap' }}>
          Σratios 10m:
        </span>
        <div style={{ flex: 1, height: 4, background: COLORS.border, borderRadius: 2, overflow: 'hidden' }}>
          <div style={{ width: `${sumBarPct}%`, height: '100%', background: sumBarColor, borderRadius: 2, transition: 'width 0.3s' }} />
        </div>
        <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: sumBarColor, minWidth: 60 }}>
          {sumR.toFixed(0)}/{thr} (n={nAligned})
        </span>
      </div>

      {/* Row 3: timing + silencio + perf */}
      <div style={{ display: 'flex', gap: 8, fontFamily: FONT_MONO, fontSize: 8, color: COLORS.textD, flexWrap: 'wrap' }}>
        {ts && <span style={{ color: COLORS.mute }}>{ts}</span>}
        <span>sil {silMin}m{silSec}s</span>
        {wr5 && <span>{wr5}</span>}
        {pnl2h && <span style={{ color: regimeData.pnl_2h >= 0 ? COLORS.green : COLORS.red }}>{pnl2h}</span>}
        {loss && <span style={{ color: regimeData.consecutive_losses >= 2 ? COLORS.orange : COLORS.textD }}>loss {loss}</span>}
      </div>
    </div>
  );
}
