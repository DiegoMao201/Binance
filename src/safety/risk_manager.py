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

        return RiskSnapshot(
            balance_usd=round(float(balance_usd), 4),
            equity_usd=round(equity, 4),
            high_water_mark=round(hwm, 4),
            max_trade_usd=max_trade_usd,
            recommended_trade_usd=recommended_trade_usd,
            drawdown_pct=round(drawdown, 6),
            daily_pnl_pct=round(daily_pnl_pct, 6),
            kill_switch_triggered=drawdown >= self.settings.kill_switch_drawdown,
        )

    def compute_trade_notional(self, equity_usd: float) -> float:
        minimum_viable = self.settings.minimum_trade_usdt
        if equity_usd < minimum_viable:
            return 0.0
        # Usa el porcentaje configurado del equity, con buffer para fees.
        target = equity_usd * self.position_size_pct * self.notional_buffer
        return round(max(target, minimum_viable), 4)

    def compute_order_size(self, price: float, equity_usd: float) -> float:
        trade_capital = self.compute_trade_notional(equity_usd)
        if price <= 0:
            return 0.0
        return round(trade_capital / price, 6)

    def build_protection_levels(self, price: float, side: str) -> dict[str, float]:
        if side == "buy":
            stop_loss = price * (1 - self.settings.stop_loss_pct)
            take_profit = price * (1 + self.settings.take_profit_pct)
        else:
            stop_loss = price * (1 + self.settings.stop_loss_pct)
            take_profit = price * (1 - self.settings.take_profit_pct)

        return {
            "stop_loss": round(stop_loss, 4),
            "take_profit": round(take_profit, 4),
        }