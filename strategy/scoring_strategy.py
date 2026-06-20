from __future__ import annotations

from dataclasses import dataclass
import pandas as pd

from app.config import Settings
from strategy.indicators import ema, rsi, atr


@dataclass
class Signal:
    decision: str
    side: str | None
    score: float
    regime: str
    bias: str
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    rr: float | None
    reasons: list[str]


@dataclass
class ImbalanceZone:
    kind: str
    lower: float
    upper: float
    mid: float
    age: int


@dataclass
class LiquidityContext:
    prev_high: float
    prev_low: float
    buy_side_sweep: bool
    sell_side_sweep: bool
    close_back_inside_from_high: bool
    close_back_inside_from_low: bool


class ScoringStrategy:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.s.ema_fast)
        df["ema_slow"] = ema(df["close"], self.s.ema_slow)
        df["ema_trend"] = ema(df["close"], self.s.ema_trend)
        df["rsi"] = rsi(df["close"], self.s.rsi_period)
        df["atr"] = atr(df, self.s.atr_period)
        df["volume_ma"] = df["volume"].rolling(self.s.volume_ma_period).mean()
        return df

    def find_latest_imbalance(self, df: pd.DataFrame, lookback: int = 80) -> ImbalanceZone | None:
        """
        Ищем последний FVG / imbalance по 3-свечной логике.

        Bullish FVG:
            candle_1.high < candle_3.low

        Bearish FVG:
            candle_1.low > candle_3.high
        """
        if len(df) < 5:
            return None

        recent = df.tail(lookback).copy()

        zones: list[ImbalanceZone] = []

        for i in range(2, len(recent)):
            c1 = recent.iloc[i - 2]
            c3 = recent.iloc[i]

            c1_high = float(c1["high"])
            c1_low = float(c1["low"])
            c3_high = float(c3["high"])
            c3_low = float(c3["low"])

            # Bullish imbalance / FVG
            if c1_high < c3_low:
                lower = c1_high
                upper = c3_low
                mid = (lower + upper) / 2
                zones.append(
                    ImbalanceZone(
                        kind="bullish",
                        lower=lower,
                        upper=upper,
                        mid=mid,
                        age=len(recent) - i,
                    )
                )

            # Bearish imbalance / FVG
            if c1_low > c3_high:
                lower = c3_high
                upper = c1_low
                mid = (lower + upper) / 2
                zones.append(
                    ImbalanceZone(
                        kind="bearish",
                        lower=lower,
                        upper=upper,
                        mid=mid,
                        age=len(recent) - i,
                    )
                )

        if not zones:
            return None

        # Берём самый свежий имбаланс
        return zones[-1]

    def liquidity_context(self, df: pd.DataFrame, lookback: int = 30) -> LiquidityContext:
        """
        Простая модель ликвидности:
        - buy-side liquidity выше предыдущих high;
        - sell-side liquidity ниже предыдущих low;
        - sweep = прокол уровня и закрытие обратно внутрь диапазона.
        """
        if len(df) < lookback + 2:
            lookback = max(5, len(df) - 2)

        last = df.iloc[-1]
        previous_range = df.iloc[-lookback - 1 : -1]

        prev_high = float(previous_range["high"].max())
        prev_low = float(previous_range["low"].min())

        last_high = float(last["high"])
        last_low = float(last["low"])
        last_close = float(last["close"])

        buy_side_sweep = last_high > prev_high and last_close < prev_high
        sell_side_sweep = last_low < prev_low and last_close > prev_low

        return LiquidityContext(
            prev_high=prev_high,
            prev_low=prev_low,
            buy_side_sweep=buy_side_sweep,
            sell_side_sweep=sell_side_sweep,
            close_back_inside_from_high=buy_side_sweep,
            close_back_inside_from_low=sell_side_sweep,
        )

    def price_reacts_from_imbalance(
        self,
        last: pd.Series,
        zone: ImbalanceZone | None,
    ) -> tuple[bool, bool, list[str]]:
        """
        Проверяем реакцию от 50% зоны имбаланса.

        Long:
            цена коснулась 50% bullish imbalance и закрылась выше mid.

        Short:
            цена коснулась 50% bearish imbalance и закрылась ниже mid.
        """
        reasons: list[str] = []

        if zone is None:
            return False, False, reasons

        last_low = float(last["low"])
        last_high = float(last["high"])
        last_close = float(last["close"])

        bullish_reaction = False
        bearish_reaction = False

        if zone.kind == "bullish":
            touched_mid = last_low <= zone.mid <= last_high
            closed_above_mid = last_close > zone.mid

            if touched_mid and closed_above_mid:
                bullish_reaction = True
                reasons.append(
                    f"bullish_imbalance_50_reaction={zone.lower:.2f}-{zone.upper:.2f}"
                )

        if zone.kind == "bearish":
            touched_mid = last_low <= zone.mid <= last_high
            closed_below_mid = last_close < zone.mid

            if touched_mid and closed_below_mid:
                bearish_reaction = True
                reasons.append(
                    f"bearish_imbalance_50_reaction={zone.lower:.2f}-{zone.upper:.2f}"
                )

        return bullish_reaction, bearish_reaction, reasons

    def analyze(self, raw_df: pd.DataFrame, ticker: dict) -> Signal:
        df = self.enrich(raw_df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last["close"])
        bid = float(ticker["bid"])
        ask = float(ticker["ask"])

        spread_pct = ((ask - bid) / price) * 100 if price else 999

        score_long = 0.0
        score_short = 0.0
        reasons_long: list[str] = []
        reasons_short: list[str] = []
        common_reasons: list[str] = []

        if spread_pct > self.s.max_spread_pct:
            return Signal(
                "no_entry",
                None,
                0,
                "unsafe",
                "none",
                None,
                None,
                None,
                None,
                [f"spread_too_high={spread_pct:.4f}%"],
            )

        atr_value = float(last["atr"]) if pd.notna(last["atr"]) else 0.0

        if atr_value <= 0:
            return Signal(
                "no_entry",
                None,
                0,
                "unknown",
                "none",
                None,
                None,
                None,
                None,
                ["atr_not_ready"],
            )

        atr_pct = atr_value / price * 100
        regime = "trend" if atr_pct >= 0.25 else "range"

        # =========================
        # 1. EMA trend logic
        # =========================

        if last["ema_fast"] > last["ema_slow"] > last["ema_trend"] and price > last["ema_trend"]:
            score_long += 3.0
            reasons_long.append("long_trend_ema_stack")

        if last["ema_fast"] < last["ema_slow"] < last["ema_trend"] and price < last["ema_trend"]:
            score_short += 3.0
            reasons_short.append("short_trend_ema_stack")

        if last["ema_fast"] > prev["ema_fast"] and last["ema_slow"] > prev["ema_slow"]:
            score_long += 1.0
            reasons_long.append("ema_slope_up")

        if last["ema_fast"] < prev["ema_fast"] and last["ema_slow"] < prev["ema_slow"]:
            score_short += 1.0
            reasons_short.append("ema_slope_down")

        # =========================
        # 2. RSI directional filter
        # =========================

        rsi_value = float(last["rsi"]) if pd.notna(last["rsi"]) else 50.0

        if 50 <= rsi_value <= 68:
            score_long += 1.2
            reasons_long.append(f"rsi_long_zone={rsi_value:.1f}")

        if 32 <= rsi_value <= 50:
            score_short += 1.2
            reasons_short.append(f"rsi_short_zone={rsi_value:.1f}")

        # =========================
        # 3. Volume confirmation
        # =========================

        volume_ma = float(last["volume_ma"]) if pd.notna(last["volume_ma"]) else 0.0
        current_volume = float(last["volume"])

        if volume_ma and current_volume > volume_ma * 1.05:
            score_long += 0.8
            score_short += 0.8
            common_reasons.append("volume_above_average")

        # =========================
        # 4. Local range position
        # =========================

        lookback = df.tail(21).iloc[:-1]
        recent_high = float(lookback["high"].max())
        recent_low = float(lookback["low"].min())
        mid_range = (recent_high + recent_low) / 2

        if price > mid_range:
            score_long += 0.6
            reasons_long.append("price_upper_half_range")
        else:
            score_short += 0.6
            reasons_short.append("price_lower_half_range")

        # =========================
        # 5. Liquidity sweep logic
        # =========================

        liq = self.liquidity_context(df, lookback=30)

        if liq.sell_side_sweep:
            score_long += 3.0
            reasons_long.append(
                f"sell_side_liquidity_sweep prev_low={liq.prev_low:.2f}"
            )

        if liq.buy_side_sweep:
            score_short += 3.0
            reasons_short.append(
                f"buy_side_liquidity_sweep prev_high={liq.prev_high:.2f}"
            )

        # =========================
        # 6. Imbalance / FVG logic
        # =========================

        imbalance = self.find_latest_imbalance(df, lookback=80)
        bullish_imbalance_reaction, bearish_imbalance_reaction, imbalance_reasons = (
            self.price_reacts_from_imbalance(last, imbalance)
        )

        if bullish_imbalance_reaction:
            score_long += 2.5
            reasons_long.extend(imbalance_reasons)

        if bearish_imbalance_reaction:
            score_short += 2.5
            reasons_short.extend(imbalance_reasons)

        # =========================
        # 7. Range filter
        # =========================

        has_long_liquidity_and_imbalance = liq.sell_side_sweep and bullish_imbalance_reaction
        has_short_liquidity_and_imbalance = liq.buy_side_sweep and bearish_imbalance_reaction

        if regime == "range":
            if not has_long_liquidity_and_imbalance:
                score_long -= 4.0
                reasons_long.append("range_requires_liquidity_and_imbalance")

            if not has_short_liquidity_and_imbalance:
                score_short -= 4.0
                reasons_short.append("range_requires_liquidity_and_imbalance")

        # =========================
        # 8. Side selection
        # =========================

        if score_long >= score_short:
            side = "Buy"
            score = score_long
            bias = "long"
            reasons = reasons_long + common_reasons
        else:
            side = "Sell"
            score = score_short
            bias = "short"
            reasons = reasons_short + common_reasons

        # =========================
        # 9. Entry / SL / TP
        # =========================

        min_rr = float(self.s.min_rr)

        if side == "Buy":
            entry = ask * (1 + self.s.slippage_pct / 100)

            base_stop = entry - atr_value * 1.5

            # Если был sweep ликвидности — стоп лучше ставить за sweep low
            if liq.sell_side_sweep:
                liquidity_stop = liq.prev_low - atr_value * 0.25
                stop_loss = min(base_stop, liquidity_stop)
            elif imbalance and imbalance.kind == "bullish":
                imbalance_stop = imbalance.lower - atr_value * 0.25
                stop_loss = min(base_stop, imbalance_stop)
            else:
                stop_loss = base_stop

            risk_distance = entry - stop_loss

            # Цель — ближайшая buy-side liquidity сверху, если RR нормальный.
            liquidity_target = liq.prev_high
            atr_target = entry + risk_distance * min_rr

            if liquidity_target > entry:
                target_rr = (liquidity_target - entry) / risk_distance
                take_profit = liquidity_target if target_rr >= min_rr else atr_target
            else:
                take_profit = atr_target

            rr = (take_profit - entry) / risk_distance

        else:
            entry = bid * (1 - self.s.slippage_pct / 100)

            base_stop = entry + atr_value * 1.5

            # Если был sweep ликвидности — стоп лучше ставить за sweep high
            if liq.buy_side_sweep:
                liquidity_stop = liq.prev_high + atr_value * 0.25
                stop_loss = max(base_stop, liquidity_stop)
            elif imbalance and imbalance.kind == "bearish":
                imbalance_stop = imbalance.upper + atr_value * 0.25
                stop_loss = max(base_stop, imbalance_stop)
            else:
                stop_loss = base_stop

            risk_distance = stop_loss - entry

            # Цель — ближайшая sell-side liquidity снизу, если RR нормальный.
            liquidity_target = liq.prev_low
            atr_target = entry - risk_distance * min_rr

            if liquidity_target < entry:
                target_rr = (entry - liquidity_target) / risk_distance
                take_profit = liquidity_target if target_rr >= min_rr else atr_target
            else:
                take_profit = atr_target

            rr = (entry - take_profit) / risk_distance

        if risk_distance <= 0:
            return Signal(
                "no_entry",
                side,
                score,
                regime,
                bias,
                entry,
                stop_loss,
                take_profit,
                None,
                reasons + ["invalid_risk_distance"],
            )

        # =========================
        # 10. Final filters
        # =========================

        if rr + 0.05 < min_rr:
            return Signal(
                "no_entry",
                side,
                score,
                regime,
                bias,
                entry,
                stop_loss,
                take_profit,
                rr,
                reasons + [f"rr_too_low={rr:.2f}"],
            )

        if regime == "range":
            if side == "Buy":
                has_valid_setup = liq.sell_side_sweep and bullish_imbalance_reaction
            else:
                has_valid_setup = liq.buy_side_sweep and bearish_imbalance_reaction

            if not has_valid_setup:
                return Signal(
                    "no_entry",
                    side,
                    score,
                    regime,
                    bias,
                    entry,
                    stop_loss,
                    take_profit,
                    rr,
                    reasons + ["range_requires_liquidity_AND_imbalance"],
                )

            min_score = 6.0

        else:
            min_score = 6.0

        if score < min_score:
            return Signal(
                "no_entry",
                side,
                score,
                regime,
                bias,
                entry,
                stop_loss,
                take_profit,
                rr,
                reasons + [f"score_too_low need={min_score:.1f}"],
            )

        return Signal(
            "entry",
            side,
            score,
            regime,
            bias,
            entry,
            stop_loss,
            take_profit,
            rr,
            reasons,
        )