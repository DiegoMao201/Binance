//... (rest of the file remains the same)

return (
  //... (rest of the file remains the same)

  <div style={{
    display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8
  }}>
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <span style={{ fontSize: 14, fontWeight: 700, color: T.text }}>{sym}</span>
      <SideBadge side={c.side} />
    </div>
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <span style={{ fontSize: 9, color: T.mute, fontFamily: FONT_MONO }}>ATR: {n(atr, 2)} - {atr > 0.05? "Alta volatilidad" : "Baja volatilidad"}</span>
    </div>
  </div>

  //... (rest of the file remains the same)