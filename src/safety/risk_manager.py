from __future__ import annotations

from dataclasses import dataclass

from src.utils.config import Settings


@dataclass(slots=True)
class RiskSnapshot:
    balance_usd: float
    equity_usd: float
    high_water_mark: float
    max_trade_usd: float
    recommended_trade_usd: float
    drawdown_pct: float
    daily_pnl_pct: float
    kill_switch_triggered: bool


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # En esta estrategia el "riesgo" lo limita el SL fijo (1%), no el sizing.
        # Sizing = porcentaje del equity disponible (POSITION_SIZE_PCT).
        self.position_size_pct = max(0.0, min(float(getattr(settings, "position_size_pct", 1.0)), 1.0))
        # Buffer 0.5% para cubrir fees + slippage en market orders.
        self.notional_buffer = 0.995

    def evaluate(
        self,
        balance_usd: float,
        equity_usd: float | None = None,
        high_water_mark: float | None = None,
    ) -> RiskSnapshot:
        equity = float(equity_usd if equity_usd is not None else balance_usd)
        baseline = float(self.settings.initial_capital_usd)
        hwm = max(
            float(high_water_mark or 0.0),
            baseline,
            equity,
        )

        drawdown = 0.0
        if hwm > 0:
            drawdown = max(0.0, (hwm - equity) / hwm)

        max_trade_usd = round(equity * self.position_size_pct, 4)
        recommended_trade_usd = self.compute_trade_notional(equity)
        daily_pnl_pct = 0.0
        if baseline > 0:
            daily_pnl_pct = (equity - baseline) / baseline

        # Kill switch: se usa siempre el umbral configurado (KILL_SWITCH_DRAWDOWN).
        # El override agresivo del 4% para cuentas < $25 fue eliminado porque causaba
        # disparos falsos cuando el equity caía temporalmente por debajo del umbral
        # (e.g. posición abierta consume USDT libre) y creaba bucles de stop/reinicio.
        effective_ks_threshold = self.settings.kill_switch_drawdown

        return RiskSnapshot(
            balance_usd=round(float(balance_usd), 4),
            equity_usd=round(equity, 4),
            high_water_mark=round(hwm, 4),
            max_trade_usd=max_trade_usd,
            recommended_trade_usd=recommended_trade_usd,
            drawdown_pct=round(drawdown, 6),
            daily_pnl_pct=round(daily_pnl_pct, 6),
            kill_switch_triggered=drawdown >= effective_ks_threshold,
        )

    def compute_trade_notional(
        self,
        equity_usd: float,
        *,
        free_quote_usd: float | None = None,
        conviction_multiplier: float | None = None,
    ) -> float:
        """Calcula el notional objetivo de la orden.

        Si se proporciona ``free_quote_usd`` (saldo libre real en la moneda
        cotizada), usamos ``min(equity * pct, free_quote * 0.95)`` como base.
        Esto evita el patron de produccion (audit 2026-05-04) en el que el bot
        calculaba sizing contra equity total ($40) pero Binance solo permitia
        gastar el USDT libre ($20), generando 10 rejected en cadena.

        ``conviction_multiplier`` (0.5-1.0, opcional) escala el tamano por
        calidad del setup: setups borderline van con la mitad del capital,
        setups premium con full size. Permite "aumentar agresividad sin
        aumentar riesgo absoluto".
        """
        minimum_viable = self.settings.minimum_trade_usdt
        if equity_usd < minimum_viable and (free_quote_usd is None or free_quote_usd < minimum_viable):
            return 0.0

        target = equity_usd * self.position_size_pct * self.notional_buffer

        if conviction_multiplier is not None:
            mult = max(0.5, min(1.0, float(conviction_multiplier)))
            target *= mult

        if free_quote_usd is not None and free_quote_usd > 0:
            spendable = free_quote_usd * 0.97
            target = min(target, spendable)

        if target < minimum_viable:
            return 0.0
        return round(max(target, minimum_viable), 4)

    def compute_order_size(
        self,
        price: float,
        equity_usd: float,
        *,
        free_quote_usd: float | None = None,
        conviction_multiplier: float | None = None,
    ) -> float:
        trade_capital = self.compute_trade_notional(
            equity_usd,
            free_quote_usd=free_quote_usd,
            conviction_multiplier=conviction_multiplier,
        )
        if price <= 0:
            return 0.0
        return round(trade_capital / price, 6)

    def build_protection_levels(
        self,
        price: float,
        side: str,
        *,
        atr_pct: float | None = None,
    ) -> dict[str, float]:
        """SL/TP base + adaptacion por volatilidad real (ATR).

        - Sin ATR: usa los porcentajes fijos del settings (comportamiento legacy).
        - Con ATR: SL = max(stop_loss_pct, 1.5 * ATR%) y TP = max(take_profit_pct,
          2.5 * ATR%). Esto garantiza que en mercados volatiles el TP no quede
          inalcanzable (caso SOL 2026-05-04 que hizo MFE +0.56% pero TP estaba
          en +3% y nunca se gatillo) y que en mercados muy quietos no nos coma
          el ruido el SL.
        - Cap de seguridad: SL nunca supera 4% (controla riesgo asimetrico).
        """
        sl_pct_base = float(self.settings.stop_loss_pct)
        tp_pct_base = float(self.settings.take_profit_pct)

        if atr_pct is not None and atr_pct > 0:
            atr = float(atr_pct)

            # ── [Dynamic SL — Sniper Mode] ──────────────────────────────────────
            # SL = max(config_sl, sniper_mult * ATR). En alta beta (WIF, DOGE),
            # el ATR refleja el ruido real. Con mult=1.5 y ATR=0.14%: SL = -0.21%.
            # Con ATR=0.50%: SL = -0.75%. Si mult==0: comportamiento legacy (SL fijo).
            sniper_mult = float(getattr(self.settings, "sniper_dynamic_sl_atr_mult", 0.0))
            if sniper_mult > 0:
                sl_pct = max(sl_pct_base, min(sniper_mult * atr, 0.04))
            else:
                sl_pct = sl_pct_base  # legacy: SL fijo configurado.
            # ── [/Dynamic SL] ──────────────────────────────────────────────────

            # ── [Dynamic TP] ───────────────────────────────────────────────────
            # TP adaptativo: cuando ATR < 0.20%, el TP estático es inalcanzable.
            # Fórmula: Dynamic_TP = MAX(1.0%, MIN(TP_base, ATR * 10))
            tp_pct = max(0.010, min(tp_pct_base, atr * 10))
            # ── [/Dynamic TP] ──────────────────────────────────────────────────

            # Hard caps.
            sl_pct = min(sl_pct, 0.04)
            tp_pct = min(tp_pct, 0.06)
        else:
            sl_pct = sl_pct_base
            tp_pct = tp_pct_base

        if side == "buy":
            stop_loss = price * (1 - sl_pct)
            take_profit = price * (1 + tp_pct)
        else:
            stop_loss = price * (1 + sl_pct)
            take_profit = price * (1 - tp_pct)

        return {
            "stop_loss": round(stop_loss, 6),
            "take_profit": round(take_profit, 6),
            "sl_pct_used": round(sl_pct, 6),
            "tp_pct_used": round(tp_pct, 6),
        }