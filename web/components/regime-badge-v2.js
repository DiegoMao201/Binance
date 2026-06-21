'use client';

// D.7.0 Regime Badge — standalone version with inline styles (no Tailwind).
// The main usage is the inline version in deriv-operator-console.js which
// has access to the T color object and FONT_MONO constant.

const COLORS = {
  green:  '#4ade80',
  amber:  '#fbbf24',
  orange: '#fb923c',
  red:    '#f87171',
  mute:   '#6b7280',
  textD:  '#9ca3af',
  bg2:    '#1f2937',
  border: '#374151',
};

const FONT_MONO = "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace";

export function RegimeBadgeV2({ regimeData }) {
  const STYLES = {
    BUENO:    { bg: COLORS.green  + '1a', border: COLORS.green  + '44', text: COLORS.green,  dot: COLORS.green,  action: 'opera todo' },
    MEDIOCRE: { bg: COLORS.amber  + '1a', border: COLORS.amber  + '44', text: COLORS.amber,  dot: COLORS.amber,  action: 'opera 1/2'  },
    DIFICIL:  { bg: '#ff9f431a',          border: '#ff9f4344',           text: COLORS.orange, dot: COLORS.orange, action: 'opera 1/3'  },
    CRITICO:  { bg: COLORS.red    + '1a', border: COLORS.red    + '44', text: COLORS.red,    dot: COLORS.red,    action: 'opera 1/4'  },
  };

  if (!regimeData?.regime) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', gap: 7,
        padding: '5px 10px', borderRadius: 5,
        background: COLORS.bg2, border: `1px solid ${COLORS.border}`,
      }}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: COLORS.mute, display: 'inline-block' }} />
        <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: COLORS.mute }}>RÉGIMEN D.7.0 — sin datos</span>
      </div>
    );
  }

  const st = STYLES[regimeData.regime] || STYLES.MEDIOCRE;
  const skip  = regimeData.skip_rate ?? 0;
  const cnt   = regimeData.pending_intent_counter ?? 0;
  const wr5   = regimeData.wr_5 != null ? `WR5 ${regimeData.wr_5}%` : null;
  const pnl2h = regimeData.pnl_2h != null ? `PnL ${regimeData.pnl_2h >= 0 ? '+' : ''}${Number(regimeData.pnl_2h).toFixed(2)}` : null;
  const loss  = regimeData.consecutive_losses != null ? `L${regimeData.consecutive_losses}` : null;
  const aln   = regimeData.aligned_per_h != null ? `${regimeData.aligned_per_h}/h` : null;
  const ts    = regimeData.timing_state ? regimeData.timing_state.replace(/_/g, ' ') : null;

  return (
    <div style={{
      padding: '5px 10px', borderRadius: 5,
      background: st.bg, border: `1px solid ${st.border}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%', background: st.dot, display: 'inline-block',
            boxShadow: skip > 0 ? `0 0 5px ${st.dot}` : 'none',
          }} />
          <span style={{ fontFamily: FONT_MONO, fontSize: 10, fontWeight: 800, color: st.text, letterSpacing: '0.05em' }}>
            {regimeData.regime}
          </span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 9, color: st.text, opacity: 0.75 }}>
            · {st.action}
          </span>
        </div>
        {ts && <span style={{ fontFamily: FONT_MONO, fontSize: 8, color: COLORS.mute }}>{ts}</span>}
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 2, fontFamily: FONT_MONO, fontSize: 8, color: COLORS.textD, flexWrap: 'wrap' }}>
        {wr5 && <span>{wr5}</span>}
        {pnl2h && <span style={{ color: regimeData.pnl_2h >= 0 ? COLORS.green : COLORS.red }}>{pnl2h}</span>}
        {loss && <span style={{ color: regimeData.consecutive_losses >= 2 ? COLORS.orange : COLORS.textD }}>loss {loss}</span>}
        {aln && <span>aln {aln}</span>}
        <span style={{ color: COLORS.mute }}>intents {cnt}</span>
      </div>
    </div>
  );
}
