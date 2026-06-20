from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderbookContext:
    bid_volume: float
    ask_volume: float
    imbalance_ratio: float
    spread: float
    spread_pct: float
    nearest_bid: float | None
    nearest_ask: float | None
    buy_wall_price: float | None
    buy_wall_size: float
    sell_wall_price: float | None
    sell_wall_size: float
    pressure: str
    reasons: list[str]


class OrderbookAnalyzer:
    def analyze(self, orderbook: dict, current_price: float) -> OrderbookContext:
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        reasons: list[str] = []

        if not bids or not asks:
            return OrderbookContext(
                bid_volume=0.0,
                ask_volume=0.0,
                imbalance_ratio=1.0,
                spread=0.0,
                spread_pct=999.0,
                nearest_bid=None,
                nearest_ask=None,
                buy_wall_price=None,
                buy_wall_size=0.0,
                sell_wall_price=None,
                sell_wall_size=0.0,
                pressure="unknown",
                reasons=["orderbook_empty"],
            )

        nearest_bid = bids[0][0]
        nearest_ask = asks[0][0]

        spread = nearest_ask - nearest_bid
        spread_pct = (spread / current_price) * 100 if current_price else 999.0

        # Берём ближайшую глубину стакана
        bid_volume = sum(size for _, size in bids)
        ask_volume = sum(size for _, size in asks)

        if ask_volume <= 0:
            imbalance_ratio = 999.0
        else:
            imbalance_ratio = bid_volume / ask_volume

        # Ищем самые крупные стенки
        buy_wall_price, buy_wall_size = max(bids, key=lambda x: x[1])
        sell_wall_price, sell_wall_size = max(asks, key=lambda x: x[1])

        pressure = "neutral"

        if imbalance_ratio >= 1.35:
            pressure = "buy_pressure"
            reasons.append(f"orderbook_buy_pressure ratio={imbalance_ratio:.2f}")

        elif imbalance_ratio <= 0.75:
            pressure = "sell_pressure"
            reasons.append(f"orderbook_sell_pressure ratio={imbalance_ratio:.2f}")

        else:
            reasons.append(f"orderbook_neutral ratio={imbalance_ratio:.2f}")

        # Стенка рядом с ценой
        if buy_wall_price and current_price:
            buy_wall_distance_pct = abs(current_price - buy_wall_price) / current_price * 100

            if buy_wall_distance_pct <= 0.20 and buy_wall_size > (bid_volume / max(len(bids), 1)) * 2:
                reasons.append(
                    f"near_buy_wall price={buy_wall_price:.2f} distance={buy_wall_distance_pct:.3f}%"
                )

        if sell_wall_price and current_price:
            sell_wall_distance_pct = abs(sell_wall_price - current_price) / current_price * 100

            if sell_wall_distance_pct <= 0.20 and sell_wall_size > (ask_volume / max(len(asks), 1)) * 2:
                reasons.append(
                    f"near_sell_wall price={sell_wall_price:.2f} distance={sell_wall_distance_pct:.3f}%"
                )

        return OrderbookContext(
            bid_volume=bid_volume,
            ask_volume=ask_volume,
            imbalance_ratio=imbalance_ratio,
            spread=spread,
            spread_pct=spread_pct,
            nearest_bid=nearest_bid,
            nearest_ask=nearest_ask,
            buy_wall_price=buy_wall_price,
            buy_wall_size=buy_wall_size,
            sell_wall_price=sell_wall_price,
            sell_wall_size=sell_wall_size,
            pressure=pressure,
            reasons=reasons,
        )