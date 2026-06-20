from __future__ import annotations

from dataclasses import dataclass
import pandas as pd

from strategy.indicators import ema, rsi, atr


@dataclass
class TimeframeContext:
    timeframe: str
    trend: str
    price: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    rsi: float
    atr_pct: float
    recent_high: float
    recent_low: float
    bias_score: float
    reasons: list[str]


@dataclass
class MultiTimeframeContext:
    contexts: dict[str, TimeframeContext]
    global_bias: str
    global_score: float
    reasons: list[str]


class MarketContextBuilder:
    def __init__(
        self,
        ema_fast_period: int = 20,
        ema_slow_period: int = 50,
        ema_trend_period: int = 200,
        rsi_period: int = 14,
        atr_period: int = 14,
    ) -> None:
        self.ema_fast_period = ema_fast_period
        self.ema_slow_period = ema_slow_period
        self.ema_trend_period = ema_trend_period
        self.rsi_period = rsi_period
        self.atr_period = atr_period

    def build_tf_context(self, timeframe: str, df: pd.DataFrame) -> TimeframeContext:
        df = df.copy()

        df["ema_fast"] = ema(df["close"], self.ema_fast_period)
        df["ema_slow"] = ema(df["close"], self.ema_slow_period)
        df["ema_trend"] = ema(df["close"], self.ema_trend_period)
        df["rsi"] = rsi(df["close"], self.rsi_period)
        df["atr"] = atr(df, self.atr_period)

        last = df.iloc[-1]

        price = float(last["close"])
        ema_fast_value = float(last["ema_fast"])
        ema_slow_value = float(last["ema_slow"])
        ema_trend_value = float(last["ema_trend"])
        rsi_value = float(last["rsi"]) if pd.notna(last["rsi"]) else 50.0
        atr_value = float(last["atr"]) if pd.notna(last["atr"]) else 0.0
        atr_pct = atr_value / price * 100 if price else 0.0

        recent = df.tail(50)
        recent_high = float(recent["high"].max())
        recent_low = float(recent["low"].min())

        bias_score = 0.0
        reasons: list[str] = []

        if ema_fast_value > ema_slow_value > ema_trend_value and price > ema_trend_value:
            bias_score += 2.0
            reasons.append(f"{timeframe}_bullish_ema_stack")

        if ema_fast_value < ema_slow_value < ema_trend_value and price < ema_trend_value:
            bias_score -= 2.0
            reasons.append(f"{timeframe}_bearish_ema_stack")

        if rsi_value >= 55:
            bias_score += 0.5
            reasons.append(f"{timeframe}_rsi_bullish={rsi_value:.1f}")

        if rsi_value <= 45:
            bias_score -= 0.5
            reasons.append(f"{timeframe}_rsi_bearish={rsi_value:.1f}")

        if price > (recent_high + recent_low) / 2:
            bias_score += 0.3
            reasons.append(f"{timeframe}_upper_half_range")

        else:
            bias_score -= 0.3
            reasons.append(f"{timeframe}_lower_half_range")

        if bias_score >= 1.5:
            trend = "bullish"
        elif bias_score <= -1.5:
            trend = "bearish"
        else:
            trend = "mixed"

        return TimeframeContext(
            timeframe=timeframe,
            trend=trend,
            price=price,
            ema_fast=ema_fast_value,
            ema_slow=ema_slow_value,
            ema_trend=ema_trend_value,
            rsi=rsi_value,
            atr_pct=atr_pct,
            recent_high=recent_high,
            recent_low=recent_low,
            bias_score=bias_score,
            reasons=reasons,
        )

    def build(self, candles_by_tf: dict[str, pd.DataFrame]) -> MultiTimeframeContext:
        contexts: dict[str, TimeframeContext] = {}
        reasons: list[str] = []

        weighted_score = 0.0

        weights = {
            "W": 2.0,
            "D": 2.0,
            "240": 1.7,
            "60": 1.5,
            "30": 1.0,
            "15": 1.0,
            "5": 0.8,
            "1": 0.5,
        }

        for tf, df in candles_by_tf.items():
            if df is None or len(df) < 220:
                continue

            ctx = self.build_tf_context(tf, df)
            contexts[tf] = ctx

            weight = weights.get(tf, 1.0)
            weighted_score += ctx.bias_score * weight
            reasons.extend(ctx.reasons)

        if weighted_score >= 3.0:
            global_bias = "bullish"
        elif weighted_score <= -3.0:
            global_bias = "bearish"
        else:
            global_bias = "mixed"

        return MultiTimeframeContext(
            contexts=contexts,
            global_bias=global_bias,
            global_score=weighted_score,
            reasons=reasons,
        )