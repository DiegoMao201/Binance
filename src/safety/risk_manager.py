from __future__ import annotations

from dataclasses import dataclass

from src.utils.config import Settings


@dataclass(slots=True)
class RiskSnapshot:
    balance_usd: float
    max_trade_usd: float
    recommended_trade_usd: float
    drawdown_pct: float
    daily_pnl_pct: float
    kill_switch_triggered: bool


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.reference_balance = settings.initial_capital_usd

    def evaluate(self, balance_usd: float) -> RiskSnapshot:
        drawdown = 0.0
        if self.reference_balance > 0:
            drawdown = max(0.0, (self.reference_balance - balance_usd) / self.reference_balance)

        max_trade_usd = round(balance_usd * self.settings.max_risk_per_trade, 4)
        recommended_trade_usd = self.compute_trade_notional(balance_usd)
        daily_pnl_pct = 0.0
        if self.reference_balance > 0:
            daily_pnl_pct = (balance_usd - self.reference_balance) / self.reference_balance

        return RiskSnapshot(
            balance_usd=round(balance_usd, 4),
            max_trade_usd=max_trade_usd,
            recommended_trade_usd=recommended_trade_usd,
            drawdown_pct=round(drawdown, 4),
            daily_pnl_pct=round(daily_pnl_pct, 4),
            kill_switch_triggered=drawdown >= self.settings.kill_switch_drawdown,
        )

    def compute_trade_notional(self, balance_usd: float) -> float:
        policy_notional = balance_usd * self.settings.max_risk_per_trade
        minimum_viable = self.settings.minimum_trade_usdt

        if balance_usd < minimum_viable:
            return 0.0

        return round(max(policy_notional, minimum_viable), 4)

    def compute_order_size(self, price: float, balance_usd: float) -> float:
        trade_capital = self.compute_trade_notional(balance_usd)
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