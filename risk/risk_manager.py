from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class RiskState:
    balance: float
    start_day_balance: float
    trades_today: int = 0
    losing_streak: int = 0
    current_day: date = field(default_factory=date.today)


class RiskManager:
    def __init__(
        self,
        initial_balance: float,
        risk_per_trade_pct: float,
        max_daily_loss_pct: float,
        max_trades_per_day: int,
        max_losing_streak: int,
        fee_rate: float,
        max_leverage: float = 2.0,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trades_per_day = max_trades_per_day
        self.max_losing_streak = max_losing_streak
        self.fee_rate = fee_rate
        self.max_leverage = max_leverage
        self.state = RiskState(balance=initial_balance, start_day_balance=initial_balance)

    def _reset_day_if_needed(self) -> None:
        today = date.today()

        if self.state.current_day != today:
            self.state.current_day = today
            self.state.start_day_balance = self.state.balance
            self.state.trades_today = 0
            self.state.losing_streak = 0

    def can_trade(self) -> tuple[bool, str]:
        self._reset_day_if_needed()

        daily_pnl = self.state.balance - self.state.start_day_balance
        daily_loss_pct = abs(min(0, daily_pnl)) / self.state.start_day_balance * 100

        if daily_loss_pct >= self.max_daily_loss_pct:
            return False, "daily_loss_limit_reached"

        if self.state.trades_today >= self.max_trades_per_day:
            return False, "max_trades_per_day_reached"

        if self.state.losing_streak >= self.max_losing_streak:
            return False, "max_losing_streak_reached"

        return True, "risk_ok"

    def position_size(self, entry: float, stop_loss: float) -> tuple[float, float]:
        risk_usdt = self.state.balance * (self.risk_per_trade_pct / 100)
        stop_distance = abs(entry - stop_loss)

        if stop_distance <= 0:
            raise ValueError("Invalid stop distance")

        raw_qty = risk_usdt / stop_distance
        raw_notional = raw_qty * entry

        max_notional = self.state.balance * self.max_leverage

        if raw_notional > max_notional:
            notional = max_notional
            qty = notional / entry
        else:
            notional = raw_notional
            qty = raw_qty

        return qty, notional

    def register_trade_result(self, net_pnl: float) -> None:
        self.state.balance += net_pnl
        self.state.trades_today += 1

        if net_pnl < 0:
            self.state.losing_streak += 1
        else:
            self.state.losing_streak = 0