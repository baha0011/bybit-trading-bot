from __future__ import annotations

import csv
import json
import html
import sys
import fcntl
from pathlib import Path

import os
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

from app.config import settings
from app.logger import setup_logger
from exchange.bybit_client import BybitMarketClient
from strategy.scoring_strategy import ScoringStrategy
from risk.risk_manager import RiskManager
from storage.trade_store import TradeStore
from notifications.telegram_notifier import TelegramNotifier

from analysis.market_context import MarketContextBuilder
from analysis.orderbook_analyzer import OrderbookAnalyzer

logger = setup_logger("bot")

class TelegramEveryLogHandler(logging.Handler):
    def __init__(self, tg: TelegramNotifier) -> None:
        super().__init__()
        self.tg = tg

    def emit(self, record: logging.LogRecord) -> None:
        try:
            raw_msg = record.getMessage()

            # Telegram-канал логов не должен превращаться в помойку.
            # Подробные INFO-логи остаются в терминале/файле, а в TG идут только важные события.
            muted_patterns = (
                "No new closed candle yet",
                "Position open |",
                "MTF/OB |",
                "No entry:",
                "score_too_low",
                "too_far_from_ema",
                "Cooldown active | no new entries",
                "Trading blocked by risk manager: max_trades_per_day_reached",
                "Telegram control thread started",
                "New entries blocked by Telegram control",
            )

            if any(pattern in raw_msg for pattern in muted_patterns):
                return

            important_patterns = (
                "Bot started",
                "Bot stopped",
                "PAPER ENTRY",
                "PAPER EXIT",
                "BREAK-EVEN",
                "Trailing stop updated",
                "Restored open paper positions",
                "ERROR",
                "error",
                "Exception",
                "Traceback",
                "Trading blocked by risk manager",
            )

            if record.levelno < logging.WARNING and not any(pattern in raw_msg for pattern in important_patterns):
                return

            msg = self.format(record)

            chunks = [msg[i:i + 3900] for i in range(0, len(msg), 3900)]

            for chunk in chunks:
                self.tg.send(chunk)

        except Exception as exc:
            print(f"Telegram log handler error: {exc}")


def attach_telegram_to_logger(tg: TelegramNotifier) -> None:
    if not tg.enabled:
        print("Telegram logs disabled: token/chat_id empty")
        return

    # Чтобы не добавить Telegram handler два раза
    for handler in logger.handlers:
        if isinstance(handler, TelegramEveryLogHandler):
            return

    telegram_handler = TelegramEveryLogHandler(tg)
    telegram_handler.setLevel(logging.INFO)

    telegram_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )

    logger.addHandler(telegram_handler)
    logger.setLevel(logging.INFO)

    print("Telegram log handler attached")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_reasons(reasons: list[str]) -> str:
    return ", ".join(reasons)


def normalize_side(side: str) -> str:
    side_lower = str(side).lower()

    if side_lower in ("buy", "long"):
        return "long"

    if side_lower in ("sell", "short"):
        return "short"

    return side_lower


def get_last_price(ticker: dict[str, Any]) -> float:
    for key in ("last", "lastPrice", "last_price", "price", "markPrice", "indexPrice"):
        if key in ticker and ticker[key] is not None:
            return float(ticker[key])

    raise ValueError(f"Cannot find last price in ticker: {ticker}")


def should_close_position(position: dict[str, Any], current_price: float) -> tuple[bool, str]:
    side = normalize_side(position["side"])
    sl = float(position["stop_loss"])
    tp = float(position["take_profit"])

    reason = str(position.get("reason", ""))
    entry = float(position["entry"])
    initial_risk = float(position.get("initial_risk", abs(entry - sl)))
    age_min = position_age_minutes(position)

    # Сначала обычные SL/TP
    if side == "long":
        if current_price <= sl:
            return True, "SL"
        if current_price >= tp:
            return True, "TP"

    if side == "short":
        if current_price >= sl:
            return True, "SL"
        if current_price <= tp:
            return True, "TP"

    # Не инвалидируем сделку слишком рано.
    # Первые 15 минут даём сетапу "подышать".
    if age_min < 15:
        return False, ""

    # Более мягкая invalidation:
    # закрываем не на 0.5R против нас, а на 0.85R.
    if "bearish_imbalance" in reason and side == "short":
        if current_price > entry + initial_risk * 0.85:
            return True, "INVALIDATED_SETUP"

    if "bullish_imbalance" in reason and side == "long":
        if current_price < entry - initial_risk * 0.85:
            return True, "INVALIDATED_SETUP"

    return False, ""


def calculate_pnl(position: dict[str, Any], exit_price: float) -> tuple[float, float, float]:
    side = normalize_side(position["side"])
    entry = float(position["entry"])
    qty = float(position["qty"])
    fee_rate = float(settings.fee_rate)

    if side == "long":
        gross_pnl = (exit_price - entry) * qty
    elif side == "short":
        gross_pnl = (entry - exit_price) * qty
    else:
        raise ValueError(f"Unknown side: {position['side']}")

    entry_fee = entry * qty * fee_rate
    exit_fee = exit_price * qty * fee_rate
    total_fee = entry_fee + exit_fee

    net_pnl = gross_pnl - total_fee

    return gross_pnl, total_fee, net_pnl

def calculate_unrealized_pnl(position: dict[str, Any], current_price: float) -> tuple[float, float, float]:
    return calculate_pnl(position, current_price)


def position_age_minutes(position: dict[str, Any]) -> float:
    opened_at_raw = position.get("opened_at")

    if opened_at_raw is None:
        return 0.0

    opened_at = datetime.fromisoformat(opened_at_raw)
    now = datetime.now(timezone.utc)

    return (now - opened_at).total_seconds() / 60


def maybe_move_stop_to_break_even(position: dict[str, Any], current_price: float) -> bool:
    side = normalize_side(position["side"])
    entry = float(position["entry"])
    initial_risk = float(position["initial_risk"])

    if position.get("break_even_moved"):
        return False

    if initial_risk <= 0:
        return False

    if side == "long":
        one_r_reached = current_price >= entry + initial_risk

        if one_r_reached:
            position["stop_loss"] = entry
            position["break_even_moved"] = True
            return True

    if side == "short":
        one_r_reached = current_price <= entry - initial_risk

        if one_r_reached:
            position["stop_loss"] = entry
            position["break_even_moved"] = True
            return True

    return False


def maybe_apply_trailing_stop(position: dict[str, Any], current_price: float) -> bool:
    side = normalize_side(position["side"])
    entry = float(position["entry"])
    current_sl = float(position["stop_loss"])
    initial_risk = float(position["initial_risk"])

    if initial_risk <= 0:
        return False

    # trailing включаем только после движения 1.5R
    if side == "long":
        if current_price < entry + initial_risk * 1.5:
            return False

        new_sl = current_price - initial_risk * 0.8

        if new_sl > current_sl:
            position["stop_loss"] = new_sl
            position["trailing_active"] = True
            return True

    if side == "short":
        if current_price > entry - initial_risk * 1.5:
            return False

        new_sl = current_price + initial_risk * 0.8

        if new_sl < current_sl:
            position["stop_loss"] = new_sl
            position["trailing_active"] = True
            return True

    return False


def should_time_stop(position: dict[str, Any], current_price: float) -> tuple[bool, str]:
    age_min = position_age_minutes(position)

    # Range-сделки должны отрабатывать быстро.
    if position.get("regime") == "range":
        if age_min >= 45:
            return True, "TIME_STOP_RANGE"

    # Trend-сделки можно держать дольше.
    if age_min >= 120:
        return True, "TIME_STOP_TREND"

    # Максимальный лимит удержания.
    if age_min >= 180:
        return True, "MAX_HOLD_TIME"

    return False, ""


def update_risk_state_after_close(risk: RiskManager, net_pnl: float) -> float:
    """
    Обновляем виртуальный баланс и простые risk-поля.
    Сделано мягко: если каких-то полей нет в RiskManager — бот не падает.
    """
    state = risk.state

    if hasattr(state, "balance"):
        state.balance = float(state.balance) + float(net_pnl)

    if hasattr(state, "trades_today"):
        state.trades_today = int(state.trades_today) + 1

    if hasattr(state, "daily_pnl"):
        state.daily_pnl = float(state.daily_pnl) + float(net_pnl)

    if hasattr(state, "losing_streak"):
        if net_pnl < 0:
            state.losing_streak = int(state.losing_streak) + 1
        else:
            state.losing_streak = 0

    return float(getattr(state, "balance", settings.initial_balance))



BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STORAGE_DIR = BASE_DIR / "storage"
OPEN_POSITIONS_FILE = STATE_DIR / "open_positions.json"
TRADES_CSV_PATH = STORAGE_DIR / "trades.csv"
TELEGRAM_CONTROL_STATE_FILE = STATE_DIR / "telegram_control_state.json"
TELEGRAM_CONTROL_FLAGS_FILE = STATE_DIR / "telegram_control_flags.json"


def load_latest_balance_from_trades(default_balance: float) -> float:
    """
    После перезапуска восстанавливаем paper-баланс из последней строки trades.csv.
    Это убирает проблему, когда бот после рестарта снова начинает с 10000.
    """
    if not TRADES_CSV_PATH.exists():
        return float(default_balance)

    try:
        latest_balance: float | None = None

        with TRADES_CSV_PATH.open("r", encoding="utf-8", errors="ignore", newline="") as file:
            reader = csv.DictReader(file)

            for row in reader:
                raw = str(row.get("balance_after", "")).strip()
                if not raw:
                    continue

                latest_balance = float(raw)

        return float(latest_balance) if latest_balance is not None else float(default_balance)

    except Exception as exc:
        logger.warning("Failed to restore balance from trades.csv: %s", exc)
        return float(default_balance)


def position_from_trade_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """
    Восстанавливает paper-позицию из строки trades.csv со статусом paper_open.
    Это страховка на случай, если state/open_positions.json потерялся или бот был перезапущен.
    """
    try:
        entry = float(row.get("entry", 0))
        stop_loss = float(row.get("stop_loss", 0))
        take_profit = float(row.get("take_profit", 0))
        qty = float(row.get("qty", 0))
        notional = float(row.get("notional", 0))
        score = float(row.get("score", 0))

        if entry <= 0 or stop_loss <= 0 or take_profit <= 0 or qty <= 0:
            return None

        return {
            "time": str(row.get("time", utc_now())),
            "symbol": str(row.get("symbol", "")).strip(),
            "side": str(row.get("side", "")).strip(),
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "qty": qty,
            "notional": notional,
            "score": score,
            "regime": str(row.get("regime", "")),
            "bias": str(row.get("bias", "")),
            "reason": str(row.get("reason", "")),
            "opened_at": str(row.get("time", utc_now())),
            "initial_stop_loss": stop_loss,
            "initial_risk": abs(entry - stop_loss),
            "break_even_moved": False,
            "trailing_active": False,
        }
    except Exception:
        return None


def load_open_positions_from_trades(symbols: list[str]) -> dict[str, dict[str, Any] | None]:
    """
    Фолбэк-восстановление открытых позиций из storage/trades.csv.

    Логика:
    - идём по CSV сверху вниз;
    - paper_open ставит позицию по символу;
    - paper_closed_* очищает позицию по символу;
    - в конце остаются только реально не закрытые позиции.
    """
    positions: dict[str, dict[str, Any] | None] = {symbol: None for symbol in symbols}

    if not TRADES_CSV_PATH.exists():
        return positions

    try:
        with TRADES_CSV_PATH.open("r", encoding="utf-8", errors="ignore", newline="") as file:
            reader = csv.DictReader(file)

            for row in reader:
                symbol = str(row.get("symbol", "")).strip().upper()
                status = str(row.get("status", "")).strip().lower()

                if symbol not in positions:
                    continue

                if status == "paper_open":
                    recovered = position_from_trade_row(row)
                    if recovered is not None:
                        positions[symbol] = recovered

                elif status.startswith("paper_closed"):
                    positions[symbol] = None

    except Exception as exc:
        logger.warning("Failed to rebuild open positions from trades.csv: %s", exc)

    return positions


def load_open_positions(symbols: list[str]) -> dict[str, dict[str, Any] | None]:
    """
    Восстанавливаем открытые paper-позиции после перезапуска.

    Основной источник: state/open_positions.json.
    Фолбэк: storage/trades.csv, если state-файл отсутствует/сломался.
    """
    positions: dict[str, dict[str, Any] | None] = {symbol: None for symbol in symbols}

    if OPEN_POSITIONS_FILE.exists():
        try:
            raw = json.loads(OPEN_POSITIONS_FILE.read_text(encoding="utf-8"))

            if isinstance(raw, dict):
                for symbol in symbols:
                    value = raw.get(symbol)
                    if isinstance(value, dict):
                        positions[symbol] = value

        except Exception as exc:
            logger.warning("Failed to load open positions state: %s", exc)

    csv_positions = load_open_positions_from_trades(symbols)

    # Если state пустой по символу, но в CSV есть незакрытая позиция — восстанавливаем из CSV.
    for symbol in symbols:
        if positions.get(symbol) is None and csv_positions.get(symbol) is not None:
            positions[symbol] = csv_positions[symbol]

    return positions


def has_unclosed_position_in_trades(symbol: str) -> bool:
    """
    Защита от дубля: перед новым входом проверяем CSV.
    Если последняя запись по символу — paper_open, значит позиция уже есть,
    даже если open_positions по какой-то причине пустой.
    """
    return load_open_positions_from_trades([symbol]).get(symbol) is not None


def save_open_positions(open_positions: dict[str, dict[str, Any] | None]) -> None:
    """
    Сохраняем открытые позиции на диск после входа, BE/trailing и закрытия.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        OPEN_POSITIONS_FILE.write_text(
            json.dumps(open_positions, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to save open positions state: %s", exc)


def close_status_from_exit(exit_reason: str, net_pnl: float) -> str:
    """
    Если SL сработал после BE/trailing и net_pnl положительный,
    в CSV лучше писать не paper_closed_sl, а более честный статус.
    """
    reason = exit_reason.lower()

    if exit_reason == "SL" and net_pnl > 0:
        return "paper_closed_trailing_profit"

    if exit_reason == "SL" and abs(net_pnl) < 1e-9:
        return "paper_closed_break_even"

    return f"paper_closed_{reason}"



def safe_read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def safe_write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to write json state %s: %s", path, exc)


def load_telegram_control_state() -> dict[str, Any]:
    state = safe_read_json(
        TELEGRAM_CONTROL_STATE_FILE,
        {
            "offset": None,
            "control_chat_id": os.getenv("TELEGRAM_CHAT_ID_CONTROL", "").strip(),
        },
    )

    if not isinstance(state, dict):
        state = {}

    state.setdefault("offset", None)
    state.setdefault("control_chat_id", os.getenv("TELEGRAM_CHAT_ID_CONTROL", "").strip())
    return state


def save_telegram_control_state(state: dict[str, Any]) -> None:
    safe_write_json(TELEGRAM_CONTROL_STATE_FILE, state)


def load_telegram_flags() -> dict[str, Any]:
    flags = safe_read_json(
        TELEGRAM_CONTROL_FLAGS_FILE,
        {
            "pause_new_entries": False,
            "emergency_stop": False,
            "updated_at": utc_now(),
        },
    )

    if not isinstance(flags, dict):
        flags = {}

    flags.setdefault("pause_new_entries", False)
    flags.setdefault("emergency_stop", False)
    flags.setdefault("updated_at", utc_now())
    return flags


def save_telegram_flags(flags: dict[str, Any]) -> None:
    flags["updated_at"] = utc_now()
    safe_write_json(TELEGRAM_CONTROL_FLAGS_FILE, flags)


def telegram_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Статус", "callback_data": "status"},
                {"text": "💰 Баланс / PnL", "callback_data": "balance"},
            ],
            [
                {"text": "📈 Позиции", "callback_data": "positions"},
                {"text": "🧾 Последние сделки", "callback_data": "trades"},
            ],
            [
                {"text": "🧠 Что видит бот", "callback_data": "market"},
                {"text": "📁 Выгрузить trades.csv", "callback_data": "file_trades"},
            ],
            [
                {"text": "⏸ Пауза входов", "callback_data": "pause"},
                {"text": "▶️ Возобновить", "callback_data": "resume"},
            ],
            [
                {"text": "⚠️ Emergency stop", "callback_data": "emergency"},
                {"text": "🔄 Обновить меню", "callback_data": "menu"},
            ],
        ]
    }


def telegram_trade_store_path(store: TradeStore) -> Path:
    try:
        return Path(store.path)
    except Exception:
        return Path("storage/trades.csv")


def read_trade_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as file:
            return list(csv.DictReader(file))
    except Exception:
        return []


def short_money(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} USDT"


def build_status_message(symbols: list[str], risk: RiskManager) -> str:
    flags = load_telegram_flags()

    return (
        "<b>📊 Статус BhTrade Bot</b>\n\n"
        f"Режим: <b>{html.escape(settings.bot_mode)}</b>\n"
        f"Биржа: <b>{html.escape(settings.bybit_env)}</b>\n"
        f"Символы: <code>{html.escape(','.join(symbols))}</code>\n"
        f"TF: <b>{html.escape(str(settings.timeframe))}m</b>\n"
        f"Баланс: <b>{float(risk.state.balance):.2f} USDT</b>\n"
        f"Сделок сегодня: <b>{getattr(risk.state, 'trades_today', 0)}</b>\n"
        f"Losing streak: <b>{getattr(risk.state, 'losing_streak', 0)}</b>\n"
        f"Пауза входов: <b>{'ON' if flags.get('pause_new_entries') else 'OFF'}</b>\n"
        f"Emergency: <b>{'ON' if flags.get('emergency_stop') else 'OFF'}</b>"
    )


def build_balance_message(store: TradeStore, risk: RiskManager) -> str:
    rows = read_trade_rows(telegram_trade_store_path(store))
    closed_rows = [row for row in rows if "closed" in str(row.get("status", ""))]
    wins = 0
    losses = 0
    net_total = 0.0
    last_balance = float(risk.state.balance)

    prev_balance: float | None = None

    for row in rows:
        raw_balance = str(row.get("balance_after", "")).strip()
        if raw_balance:
            try:
                balance = float(raw_balance)
                if prev_balance is not None and "closed" in str(row.get("status", "")):
                    pnl = balance - prev_balance
                    net_total += pnl
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1
                prev_balance = balance
                last_balance = balance
            except ValueError:
                pass

    total = wins + losses
    winrate = (wins / total * 100) if total else 0.0

    return (
        "<b>💰 Баланс / PnL</b>\n\n"
        f"Текущий paper-баланс: <b>{last_balance:.2f} USDT</b>\n"
        f"Net по закрытым сделкам: <b>{short_money(net_total)}</b>\n"
        f"Закрытых сделок: <b>{len(closed_rows)}</b>\n"
        f"Winrate: <b>{winrate:.1f}%</b>\n"
        f"Wins/Losses: <b>{wins}/{losses}</b>"
    )


def build_positions_message(open_positions: dict[str, dict[str, Any] | None]) -> str:
    active = [(symbol, pos) for symbol, pos in open_positions.items() if pos is not None]

    if not active:
        return "<b>📈 Открытые позиции</b>\n\nНет открытых paper-позиций."

    parts = ["<b>📈 Открытые позиции</b>"]

    for symbol, pos in active:
        assert pos is not None
        parts.append(
            "\n"
            f"<b>{html.escape(symbol)}</b> / <b>{html.escape(str(pos.get('side', '')))}</b>\n"
            f"Entry: <code>{float(pos.get('entry', 0)):.4f}</code>\n"
            f"SL: <code>{float(pos.get('stop_loss', 0)):.4f}</code>\n"
            f"TP: <code>{float(pos.get('take_profit', 0)):.4f}</code>\n"
            f"Qty: <code>{float(pos.get('qty', 0)):.6f}</code>\n"
            f"Score: <b>{float(pos.get('score', 0)):.2f}</b>\n"
            f"Regime: <b>{html.escape(str(pos.get('regime', '')))}</b>\n"
            f"Reason: <code>{html.escape(str(pos.get('reason', ''))[:500])}</code>"
        )

    return "\n".join(parts)


def build_recent_trades_message(store: TradeStore, limit: int = 8) -> str:
    rows = read_trade_rows(telegram_trade_store_path(store))

    if not rows:
        return "<b>🧾 Последние сделки</b>\n\nФайл сделок пока пуст."

    latest = rows[-limit:]
    parts = ["<b>🧾 Последние сделки</b>"]

    for row in reversed(latest):
        status = str(row.get("status", ""))
        symbol = str(row.get("symbol", ""))
        side = str(row.get("side", ""))
        balance = str(row.get("balance_after", ""))
        entry = str(row.get("entry", ""))
        reason = str(row.get("reason", ""))[:180]

        emoji = "✅" if ("tp" in status or "profit" in status) else "🔴" if ("sl" in status or "loss" in status) else "🟢"

        parts.append(
            "\n"
            f"{emoji} <b>{html.escape(symbol)}</b> {html.escape(side)}\n"
            f"Status: <code>{html.escape(status)}</code>\n"
            f"Entry: <code>{html.escape(entry)}</code>\n"
            f"Balance: <b>{html.escape(balance)}</b>\n"
            f"Reason: <code>{html.escape(reason)}</code>"
        )

    return "\n".join(parts)


def build_market_message(market_snapshots: dict[str, dict[str, Any]]) -> str:
    if not market_snapshots:
        return "<b>🧠 Что видит бот</b>\n\nПока нет market snapshot."

    parts = ["<b>🧠 Что видит бот сейчас</b>"]

    for symbol, snap in market_snapshots.items():
        parts.append(
            "\n"
            f"<b>{html.escape(symbol)}</b>\n"
            f"Price: <code>{html.escape(str(snap.get('price', '—')))}</code>\n"
            f"MTF: <b>{html.escape(str(snap.get('mtf_bias', 'unknown')))}</b> "
            f"score=<code>{html.escape(str(snap.get('mtf_score', '—')))}</code>\n"
            f"OB: <b>{html.escape(str(snap.get('ob_pressure', 'unknown')))}</b> "
            f"ratio=<code>{html.escape(str(snap.get('ob_ratio', '—')))}</code>\n"
            f"Signal: <b>{html.escape(str(snap.get('signal', 'waiting')))}</b>\n"
            f"Score: <code>{html.escape(str(snap.get('score', '—')))}</code> "
            f"Regime: <code>{html.escape(str(snap.get('regime', '—')))}</code>\n"
            f"Reason: <code>{html.escape(str(snap.get('reason', '—'))[:350])}</code>"
        )

    return "\n".join(parts)


def send_control_menu(chat_id: str, text: str | None = None) -> None:
    notifier = TelegramNotifier(settings.telegram_bot_token, chat_id)
    notifier.send_raw(
        text or "<b>🤖 BhTrade Bot Control</b>\n\nВыбери действие:",
        reply_markup=telegram_menu_keyboard(),
    )


def is_telegram_chat_authorized(chat_id: str, state: dict[str, Any]) -> bool:
    allowed = {
        os.getenv("TELEGRAM_CHAT_ID_CONTROL", "").strip(),
        os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        str(state.get("control_chat_id", "")).strip(),
    }
    allowed = {item for item in allowed if item}
    return str(chat_id) in allowed


def maybe_register_first_control_chat(chat_id: str, state: dict[str, Any]) -> bool:
    """
    Если TELEGRAM_CHAT_ID_CONTROL не задан, первый приватный /start станет управляющим чатом.
    После этого chat_id сохраняется в state/telegram_control_state.json.
    """
    if os.getenv("TELEGRAM_CHAT_ID_CONTROL", "").strip():
        return False

    if str(state.get("control_chat_id", "")).strip():
        return False

    state["control_chat_id"] = str(chat_id)
    save_telegram_control_state(state)
    return True


def handle_telegram_action(
    chat_id: str,
    action: str,
    open_positions: dict[str, dict[str, Any] | None],
    risk: RiskManager,
    symbols: list[str],
    store: TradeStore,
    market_snapshots: dict[str, dict[str, Any]],
) -> None:
    notifier = TelegramNotifier(settings.telegram_bot_token, chat_id)
    flags = load_telegram_flags()

    if action in ("menu", "start"):
        send_control_menu(chat_id)
        return

    if action == "status":
        notifier.send_raw(build_status_message(symbols, risk), reply_markup=telegram_menu_keyboard())
        return

    if action == "balance":
        notifier.send_raw(build_balance_message(store, risk), reply_markup=telegram_menu_keyboard())
        return

    if action == "positions":
        notifier.send_raw(build_positions_message(open_positions), reply_markup=telegram_menu_keyboard())
        return

    if action == "trades":
        notifier.send_raw(build_recent_trades_message(store), reply_markup=telegram_menu_keyboard())
        return

    if action == "market":
        notifier.send_raw(build_market_message(market_snapshots), reply_markup=telegram_menu_keyboard())
        return

    if action == "file_trades":
        path = telegram_trade_store_path(store)
        notifier.send_document(path, caption="📁 <b>trades.csv</b>")
        return

    if action == "pause":
        flags["pause_new_entries"] = True
        flags["emergency_stop"] = False
        save_telegram_flags(flags)
        notifier.send_raw("⏸ <b>Новые входы поставлены на паузу.</b>\n\nОткрытые позиции продолжают сопровождаться.", reply_markup=telegram_menu_keyboard())
        return

    if action == "resume":
        flags["pause_new_entries"] = False
        flags["emergency_stop"] = False
        save_telegram_flags(flags)
        notifier.send_raw("▶️ <b>Новые входы снова разрешены.</b>", reply_markup=telegram_menu_keyboard())
        return

    if action == "emergency":
        flags["pause_new_entries"] = True
        flags["emergency_stop"] = True
        save_telegram_flags(flags)
        notifier.send_raw(
            "⚠️ <b>Emergency stop включён.</b>\n\n"
            "Новые входы запрещены. Для возврата нажми ▶️ Возобновить.",
            reply_markup=telegram_menu_keyboard(),
        )
        return

    notifier.send_raw("Неизвестная команда. Открываю меню.", reply_markup=telegram_menu_keyboard())


def process_telegram_control_updates(
    open_positions: dict[str, dict[str, Any] | None],
    risk: RiskManager,
    symbols: list[str],
    store: TradeStore,
    market_snapshots: dict[str, dict[str, Any]],
) -> None:
    if not settings.telegram_bot_token:
        return

    state = load_telegram_control_state()
    polling_notifier = TelegramNotifier(settings.telegram_bot_token, "")
    polling_notifier.set_commands()

    updates = polling_notifier.get_updates(offset=state.get("offset"), timeout=0)

    for update in updates:
        update_id = update.get("update_id")

        if isinstance(update_id, int):
            state["offset"] = update_id + 1

        message = update.get("message")
        callback = update.get("callback_query")

        if message:
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", ""))
            text = str(message.get("text", "")).strip()

            if text.startswith("/start"):
                if maybe_register_first_control_chat(chat_id, state):
                    send_control_menu(
                        chat_id,
                        "<b>✅ Этот чат назначен управляющим.</b>\n\n"
                        "Добавь в .env для надёжности:\n"
                        f"<code>TELEGRAM_CHAT_ID_CONTROL={html.escape(chat_id)}</code>",
                    )
                    continue

            if not is_telegram_chat_authorized(chat_id, state):
                TelegramNotifier(settings.telegram_bot_token, chat_id).send_raw(
                    "🚫 Этот чат не авторизован для управления ботом."
                )
                continue

            command = text.split()[0].lower() if text else "/start"
            command_map = {
                "/start": "menu",
                "/menu": "menu",
                "/status": "status",
                "/balance": "balance",
                "/positions": "positions",
                "/trades": "trades",
                "/file": "file_trades",
                "/pause": "pause",
                "/resume": "resume",
                "/emergency": "emergency",
                "/market": "market",
            }
            handle_telegram_action(
                chat_id,
                command_map.get(command, "menu"),
                open_positions,
                risk,
                symbols,
                store,
                market_snapshots,
            )

        if callback:
            callback_id = str(callback.get("id", ""))
            data = str(callback.get("data", ""))
            message_data = callback.get("message", {})
            chat = message_data.get("chat", {})
            chat_id = str(chat.get("id", ""))

            polling_notifier.answer_callback_query(callback_id, "Выполняю...")

            if not is_telegram_chat_authorized(chat_id, state):
                TelegramNotifier(settings.telegram_bot_token, chat_id).send_raw(
                    "🚫 Этот чат не авторизован для управления ботом."
                )
                continue

            handle_telegram_action(
                chat_id,
                data,
                open_positions,
                risk,
                symbols,
                store,
                market_snapshots,
            )

    save_telegram_control_state(state)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int env %s=%s. Using default=%s", name, raw, default)
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float env %s=%s. Using default=%s", name, raw, default)
        return default


def active_open_positions(open_positions: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    return [position for position in open_positions.values() if position is not None]


def total_open_notional(open_positions: dict[str, dict[str, Any] | None]) -> float:
    total = 0.0
    for position in active_open_positions(open_positions):
        try:
            total += float(position.get("notional", 0.0))
        except Exception:
            continue
    return total


def used_margin(open_positions: dict[str, dict[str, Any] | None], max_leverage: float) -> float:
    if max_leverage <= 0:
        max_leverage = 1.0
    return total_open_notional(open_positions) / max_leverage


def capital_guard_status(
    open_positions: dict[str, dict[str, Any] | None],
    balance: float,
) -> dict[str, Any]:
    """
    Общий контроль капитала для всех символов.
    Баланс один общий, а не отдельные 100$ на каждую монету.
    """
    max_leverage = env_float("MAX_LEVERAGE", 2.0)
    max_open_positions = env_int("MAX_OPEN_POSITIONS", 1)
    max_total_exposure_pct = env_float("MAX_TOTAL_EXPOSURE_PCT", 150.0)
    max_position_notional_pct = env_float("MAX_POSITION_NOTIONAL_PCT", 120.0)
    max_margin_usage_pct = env_float("MAX_MARGIN_USAGE_PCT", 80.0)

    current_notional = total_open_notional(open_positions)
    current_used_margin = used_margin(open_positions, max_leverage)

    max_total_notional = float(balance) * (max_total_exposure_pct / 100.0)
    max_position_notional = float(balance) * (max_position_notional_pct / 100.0)
    max_allowed_margin = float(balance) * (max_margin_usage_pct / 100.0)
    free_margin = max(0.0, max_allowed_margin - current_used_margin)

    return {
        "max_leverage": max_leverage,
        "max_open_positions": max_open_positions,
        "max_total_exposure_pct": max_total_exposure_pct,
        "max_position_notional_pct": max_position_notional_pct,
        "max_margin_usage_pct": max_margin_usage_pct,
        "active_positions": len(active_open_positions(open_positions)),
        "current_notional": current_notional,
        "used_margin": current_used_margin,
        "max_total_notional": max_total_notional,
        "max_position_notional": max_position_notional,
        "max_allowed_margin": max_allowed_margin,
        "free_margin": free_margin,
    }


def can_open_position_by_capital(
    open_positions: dict[str, dict[str, Any] | None],
    balance: float,
    proposed_notional: float,
) -> tuple[bool, str, dict[str, Any]]:
    status = capital_guard_status(open_positions, balance)
    max_leverage = float(status["max_leverage"])

    if status["active_positions"] >= status["max_open_positions"]:
        return False, "max_open_positions_reached", status

    if proposed_notional > status["max_position_notional"]:
        return False, "position_notional_too_big", status

    if status["current_notional"] + proposed_notional > status["max_total_notional"]:
        return False, "total_exposure_limit_reached", status

    proposed_margin = proposed_notional / max(max_leverage, 1.0)
    if proposed_margin > status["free_margin"]:
        return False, "not_enough_free_margin", status

    return True, "capital_ok", status


def cap_position_size_by_capital(
    qty: float,
    notional: float,
    entry: float,
    open_positions: dict[str, dict[str, Any] | None],
    balance: float,
) -> tuple[float, float, str]:
    """
    Если risk.position_size дал слишком большой notional,
    мягко режем размер позиции под лимиты капитала.
    """
    status = capital_guard_status(open_positions, balance)
    max_leverage = float(status["max_leverage"])

    max_by_position = float(status["max_position_notional"])
    max_by_total = max(0.0, float(status["max_total_notional"]) - float(status["current_notional"]))
    max_by_margin = max(0.0, float(status["free_margin"]) * max(max_leverage, 1.0))
    allowed_notional = min(max_by_position, max_by_total, max_by_margin)

    if allowed_notional <= 0:
        return 0.0, 0.0, "no_capital_available"

    if notional <= allowed_notional:
        return float(qty), float(notional), "unchanged"

    capped_qty = allowed_notional / float(entry)
    return float(capped_qty), float(allowed_notional), "capped_by_capital_guard"


def parse_symbols() -> list[str]:
    raw_symbols = os.getenv("SYMBOLS", "").strip()

    if raw_symbols:
        symbols = [item.strip().upper() for item in raw_symbols.split(",") if item.strip()]
    else:
        symbols = [settings.symbol]

    # Убираем дубли, сохраняя порядок
    unique_symbols: list[str] = []
    for symbol in symbols:
        if symbol not in unique_symbols:
            unique_symbols.append(symbol)

    return unique_symbols

LOCK_FILE_HANDLE = None


def acquire_single_instance_lock() -> None:
    """
    Жёсткая защита от двойного запуска.

    Используем lock в домашней папке, а не bot.lock в проекте.
    Так второй бот не стартанёт даже если случайно запустить его из другой директории.
    """
    global LOCK_FILE_HANDLE

    lock_path = Path.home() / ".bhtrade_bybit_bot.lock"
    LOCK_FILE_HANDLE = open(lock_path, "w")

    try:
        fcntl.flock(LOCK_FILE_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)

        LOCK_FILE_HANDLE.seek(0)
        LOCK_FILE_HANDLE.truncate()
        LOCK_FILE_HANDLE.write(
            f"pid={os.getpid()}\n"
            f"cwd={os.getcwd()}\n"
            f"bot={Path(__file__).resolve()}\n"
        )
        LOCK_FILE_HANDLE.flush()

    except BlockingIOError:
        logger.error("Another bot instance is already running. Stop it first.")
        sys.exit(1)


class TelegramControlThread:
    """
    Отдельный поток для Telegram-кнопок.

    Зачем:
    - торговый цикл может долго анализировать BTC/ETH/SOL;
    - main loop спит settings.poll_seconds;
    - если Telegram проверять только в main loop, кнопки отвечают с задержкой;
    - этот поток читает Telegram каждые 1-2 секунды независимо от торговли.
    """

    def __init__(
        self,
        open_positions: dict[str, dict[str, Any] | None],
        risk: RiskManager,
        symbols: list[str],
        store: TradeStore,
        market_snapshots: dict[str, dict[str, Any]],
        interval_seconds: float = 2.0,
    ) -> None:
        self.open_positions = open_positions
        self.risk = risk
        self.symbols = symbols
        self.store = store
        self.market_snapshots = market_snapshots
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not settings.telegram_bot_token:
            logger.info("Telegram control thread disabled: TELEGRAM_BOT_TOKEN is empty")
            return

        if self._thread is not None and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run,
            name="TelegramControlThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram control thread started")

    def stop(self) -> None:
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                process_telegram_control_updates(
                    self.open_positions,
                    self.risk,
                    self.symbols,
                    self.store,
                    self.market_snapshots,
                )
            except Exception as exc:
                logger.warning("Telegram control thread error: %s", exc)

            self._stop_event.wait(self.interval_seconds)


def main() -> None:
    acquire_single_instance_lock()
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    
    client = BybitMarketClient(
        testnet=settings.testnet,
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )

    strategy = ScoringStrategy(settings)

    risk = RiskManager(
        initial_balance=settings.initial_balance,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_trades_per_day=settings.max_trades_per_day,
        max_losing_streak=settings.max_losing_streak,
        fee_rate=settings.fee_rate,
        max_leverage=env_float("MAX_LEVERAGE", 2.0),
    )

    restored_balance = load_latest_balance_from_trades(settings.initial_balance)
    risk.state.balance = restored_balance
    risk.state.start_day_balance = restored_balance

    store = TradeStore(path=str(TRADES_CSV_PATH))

    tg_logs = TelegramNotifier(
        settings.telegram_bot_token,
        os.getenv("TELEGRAM_CHAT_ID_LOGS", "").strip(),
    )

    tg_warnings = TelegramNotifier(
        settings.telegram_bot_token,
        os.getenv("TELEGRAM_CHAT_ID_WARNINGS", "").strip(),
    )

    tg_trades = TelegramNotifier(
        settings.telegram_bot_token,
        os.getenv("TELEGRAM_CHAT_ID_TRADES", "").strip(),
    )

    tg_profit = TelegramNotifier(
        settings.telegram_bot_token,
        os.getenv("TELEGRAM_CHAT_ID_PROFIT", "").strip(),
    )

    tg_loss = TelegramNotifier(
        settings.telegram_bot_token,
        os.getenv("TELEGRAM_CHAT_ID_LOSS", "").strip(),
    )

    tg = tg_logs

    market_context_builder = MarketContextBuilder(
        ema_fast_period=settings.ema_fast,
        ema_slow_period=settings.ema_slow,
        ema_trend_period=settings.ema_trend,
        rsi_period=settings.rsi_period,
        atr_period=settings.atr_period,
    )

    orderbook_analyzer = OrderbookAnalyzer()

    attach_telegram_to_logger(tg)
    logger.info("Telegram log handler test: connected")

    symbols = parse_symbols()

    start_msg = (
        f"Bot started | mode={settings.bot_mode} env={settings.bybit_env} "
        f"symbols={','.join(symbols)} timeframe={settings.timeframe} "
        f"balance={risk.state.balance:.2f} USDT "
        f"trades={TRADES_CSV_PATH}"
    )

    logger.info(start_msg)
    tg.send("🤖 " + start_msg)

    last_closed_candle_times: dict[str, Any] = {symbol: None for symbol in symbols}
    open_positions = load_open_positions(symbols)
    cooldown_until_by_symbol: dict[str, datetime | None] = {symbol: None for symbol in symbols}
    market_snapshots: dict[str, dict[str, Any]] = {symbol: {"signal": "waiting"} for symbol in symbols}

    restored_positions = [symbol for symbol, position in open_positions.items() if position is not None]
    if restored_positions:
        logger.warning("Restored open paper positions from state: %s", ", ".join(restored_positions))
        tg_warnings.send("🟡 Restored open paper positions: " + ", ".join(restored_positions))


    telegram_control_thread = TelegramControlThread(
        open_positions=open_positions,
        risk=risk,
        symbols=symbols,
        store=store,
        market_snapshots=market_snapshots,
        interval_seconds=2.0,
    )
    telegram_control_thread.start()

    control_chat_id = os.getenv("TELEGRAM_CHAT_ID_CONTROL", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if control_chat_id and settings.telegram_bot_token:
        send_control_menu(control_chat_id, "🤖 <b>BhTrade Bot запущен.</b>\n\nПанель управления готова.")

    while True:
        try:
            for symbol in symbols:
                try:
                    ticker = client.get_ticker(settings.category, symbol)
                    current_price = get_last_price(ticker)
                    market_snapshots.setdefault(symbol, {})["price"] = round(current_price, 6)

                    open_position = open_positions.get(symbol)

                    # 1. Если по этому символу есть открытая paper-позиция — сначала проверяем SL/TP.
                    if open_position is not None:
                        close_now, exit_reason = should_close_position(open_position, current_price)

                        be_moved = maybe_move_stop_to_break_even(open_position, current_price)
                        if be_moved:
                            gross_u, fee_u, net_u = calculate_unrealized_pnl(open_position, current_price)
                            age_min = position_age_minutes(open_position)

                            logger.info(
                                "%s | Position open | side=%s entry=%.4f current=%.4f sl=%.4f tp=%.4f "
                                "unrealized_net=%.4f age=%.1fmin be=%s trailing=%s",
                                symbol,
                                open_position["side"],
                                float(open_position["entry"]),
                                current_price,
                                float(open_position["stop_loss"]),
                                float(open_position["take_profit"]),
                                net_u,
                                age_min,
                                open_position.get("break_even_moved", False),
                                open_position.get("trailing_active", False),
                            )

                            tg_trades.send(
                                "🟡 BREAK-EVEN | "
                                f"{symbol} | side={open_position['side']} "
                                f"entry={float(open_position['entry']):.4f} "
                                f"new_sl={float(open_position['stop_loss']):.4f} "
                                f"current={current_price:.4f}"
                            )

                            save_open_positions(open_positions)

                        trailing_moved = maybe_apply_trailing_stop(open_position, current_price)
                        if trailing_moved:
                            logger.info(
                                "%s | Trailing stop updated | side=%s entry=%.4f new_sl=%.4f current=%.4f",
                                symbol,
                                open_position["side"],
                                float(open_position["entry"]),
                                float(open_position["stop_loss"]),
                                current_price,
                            )

                            save_open_positions(open_positions)

                        if not close_now:
                            time_close, time_reason = should_time_stop(open_position, current_price)
                            if time_close:
                                close_now = True
                                exit_reason = time_reason

                        if close_now:
                            gross_pnl, total_fee, net_pnl = calculate_pnl(open_position, current_price)
                            balance_after = update_risk_state_after_close(risk, net_pnl)

                            close_msg = (
                                f"PAPER EXIT {exit_reason} | {symbol} | "
                                f"side={open_position['side']} "
                                f"entry={float(open_position['entry']):.4f} "
                                f"exit={current_price:.4f} "
                                f"qty={float(open_position['qty']):.6f} "
                                f"gross_pnl={gross_pnl:.4f} "
                                f"fee={total_fee:.4f} "
                                f"net_pnl={net_pnl:.4f} "
                                f"balance={balance_after:.2f}"
                            )

                            if net_pnl >= 0:
                                logger.info(close_msg)
                                tg_trades.send("✅ " + close_msg)
                                tg_profit.send("✅ PROFIT | " + close_msg)
                            else:
                                logger.warning(close_msg)
                                tg_trades.send("🔴 " + close_msg)
                                tg_loss.send("🔴 LOSS | " + close_msg)

                            close_status = close_status_from_exit(exit_reason, net_pnl)

                            store.append(
                                {
                                    "time": utc_now(),
                                    "symbol": symbol,
                                    "side": open_position["side"],
                                    "entry": round(float(open_position["entry"]), 6),
                                    "exit": round(current_price, 6),
                                    "stop_loss": round(float(open_position["stop_loss"]), 6),
                                    "take_profit": round(float(open_position["take_profit"]), 6),
                                    "qty": round(float(open_position["qty"]), 8),
                                    "notional": round(float(open_position["notional"]), 2),
                                    "gross_pnl": round(gross_pnl, 6),
                                    "fee": round(total_fee, 6),
                                    "net_pnl": round(net_pnl, 6),
                                    "score": round(float(open_position["score"]), 2),
                                    "regime": open_position["regime"],
                                    "bias": open_position["bias"],
                                    "status": close_status,
                                    "reason": open_position["reason"],
                                    "balance_after": round(balance_after, 2),
                                }
                            )

                            open_positions[symbol] = None
                            save_open_positions(open_positions)
                            market_snapshots.setdefault(symbol, {}).update({
                                "signal": f"EXIT {exit_reason}",
                                "reason": f"net_pnl={net_pnl:.2f} balance={balance_after:.2f}",
                            })

                            if exit_reason == "SL":
                                cooldown_until_by_symbol[symbol] = datetime.now(timezone.utc) + timedelta(minutes=15)
                            else:
                                cooldown_until_by_symbol[symbol] = datetime.now(timezone.utc) + timedelta(minutes=5)

                            logger.info(
                                "%s | Cooldown active until %s after %s",
                                symbol,
                                cooldown_until_by_symbol[symbol].isoformat(),
                                exit_reason,
                            )

                        else:
                            logger.info(
                                "%s | Position open | side=%s entry=%.4f current=%.4f sl=%.4f tp=%.4f",
                                symbol,
                                open_position["side"],
                                float(open_position["entry"]),
                                current_price,
                                float(open_position["stop_loss"]),
                                float(open_position["take_profit"]),
                            )

                        # Если по символу есть открытая позиция, новый вход по нему не ищем.
                        continue

                    # 2. Если позиции по символу нет — анализируем новую закрытую свечу.
                    df = client.get_klines(
                        settings.category,
                        symbol,
                        settings.timeframe,
                        settings.candle_limit,
                    )

                    if len(df) < max(settings.ema_trend + 5, 220):
                        logger.info("%s | Not enough candles yet", symbol)
                        continue

                    closed_df = df.iloc[:-1].copy()
                    closed_time = closed_df.iloc[-1]["start_time"]

                    if last_closed_candle_times.get(symbol) == closed_time:
                        logger.info("%s | No new closed candle yet", symbol)
                        continue

                    last_closed_candle_times[symbol] = closed_time

                    cooldown_until = cooldown_until_by_symbol.get(symbol)

                    if cooldown_until is not None and datetime.now(timezone.utc) < cooldown_until:
                        logger.info("%s | Cooldown active | no new entries until %s", symbol, cooldown_until.isoformat())
                        continue

                    flags = load_telegram_flags()
                    if flags.get("pause_new_entries") or flags.get("emergency_stop"):
                        reason = "emergency_stop" if flags.get("emergency_stop") else "pause_new_entries"
                        logger.info("%s | New entries blocked by Telegram control: %s", symbol, reason)
                        market_snapshots.setdefault(symbol, {}).update({
                            "signal": "paused",
                            "reason": reason,
                        })
                        continue

                    capital_status = capital_guard_status(open_positions, float(risk.state.balance))
                    if capital_status["active_positions"] >= capital_status["max_open_positions"]:
                        logger.info(
                            "%s | New entry blocked by capital guard: max_open_positions_reached | "
                            "active=%s max=%s used_margin=%.2f free_margin=%.2f balance=%.2f",
                            symbol,
                            capital_status["active_positions"],
                            capital_status["max_open_positions"],
                            capital_status["used_margin"],
                            capital_status["free_margin"],
                            float(risk.state.balance),
                        )
                        if "market_snapshots" in locals():
                            market_snapshots.setdefault(symbol, {}).update({
                                "signal": "blocked",
                                "reason": "max_open_positions_reached",
                            })
                        continue

                    can_trade, risk_reason = risk.can_trade()

                    if not can_trade:
                        logger.info("%s | Trading blocked by risk manager: %s", symbol, risk_reason)
                        continue

                    mtf_timeframes = ["1", "5", "15", "30", "60", "240"]
                    candles_by_tf = {}

                    for tf in mtf_timeframes:
                        try:
                            tf_df = client.get_klines(
                                settings.category,
                                symbol,
                                tf,
                                220,
                            )

                            if len(tf_df) >= 220:
                                candles_by_tf[tf] = tf_df.iloc[:-1].copy()

                        except Exception as mtf_exc:
                            logger.warning("%s | MTF load failed | tf=%s error=%s", symbol, tf, mtf_exc)

                    mtf_context = market_context_builder.build(candles_by_tf)

                    orderbook_context = None

                    try:
                        orderbook = client.get_orderbook(
                            settings.category,
                            symbol,
                            limit=50,
                        )
                        orderbook_context = orderbook_analyzer.analyze(orderbook, current_price)

                        logger.info(
                            "%s | MTF/OB | global_bias=%s global_score=%.2f ob_pressure=%s ob_ratio=%.2f spread=%.4f%%",
                            symbol,
                            mtf_context.global_bias,
                            mtf_context.global_score,
                            orderbook_context.pressure,
                            orderbook_context.imbalance_ratio,
                            orderbook_context.spread_pct,
                        )

                        market_snapshots.setdefault(symbol, {}).update({
                            "mtf_bias": mtf_context.global_bias,
                            "mtf_score": round(float(mtf_context.global_score), 2),
                            "ob_pressure": orderbook_context.pressure,
                            "ob_ratio": round(float(orderbook_context.imbalance_ratio), 2),
                            "spread_pct": round(float(orderbook_context.spread_pct), 4),
                            "signal": "scanning",
                            "reason": "MTF/OB updated",
                        })

                    except Exception as ob_exc:
                        logger.warning("%s | Orderbook analysis failed: %s", symbol, ob_exc)

                    signal = strategy.analyze(closed_df, ticker)

                    market_snapshots.setdefault(symbol, {}).update({
                        "signal": signal.decision,
                        "score": round(float(signal.score), 2),
                        "regime": signal.regime,
                        "bias": signal.bias,
                        "reason": format_reasons(signal.reasons),
                    })

                    # V4 Multi-timeframe filter
                    if signal.decision == "entry":
                        if signal.side == "Buy" and mtf_context.global_bias == "bearish":
                            logger.info(
                                "%s | No entry: mtf_against_long | global_bias=%s global_score=%.2f reasons=%s",
                                symbol,
                                mtf_context.global_bias,
                                mtf_context.global_score,
                                ", ".join(mtf_context.reasons[-8:]),
                            )
                            continue

                        if signal.side == "Sell" and mtf_context.global_bias == "bullish":
                            logger.info(
                                "%s | No entry: mtf_against_short | global_bias=%s global_score=%.2f reasons=%s",
                                symbol,
                                mtf_context.global_bias,
                                mtf_context.global_score,
                                ", ".join(mtf_context.reasons[-8:]),
                            )
                            continue

                    # V4 Orderbook filter
                    if signal.decision == "entry" and orderbook_context is not None:
                        if signal.side == "Buy" and orderbook_context.pressure == "sell_pressure":
                            logger.info(
                                "%s | No entry: orderbook_against_long | pressure=%s ratio=%.2f reasons=%s",
                                symbol,
                                orderbook_context.pressure,
                                orderbook_context.imbalance_ratio,
                                ", ".join(orderbook_context.reasons),
                            )
                            continue

                        if signal.side == "Sell" and orderbook_context.pressure == "buy_pressure":
                            logger.info(
                                "%s | No entry: orderbook_against_short | pressure=%s ratio=%.2f reasons=%s",
                                symbol,
                                orderbook_context.pressure,
                                orderbook_context.imbalance_ratio,
                                ", ".join(orderbook_context.reasons),
                            )
                            continue

                        if orderbook_context.spread_pct > settings.max_spread_pct:
                            logger.info(
                                "%s | No entry: orderbook_spread_too_high | spread=%.4f%%",
                                symbol,
                                orderbook_context.spread_pct,
                            )
                            continue

                    if signal.decision != "entry":
                        logger.info(
                            "%s | No entry: %s | score=%.2f regime=%s bias=%s reasons=%s",
                            symbol,
                            signal.reasons[-1] if signal.reasons else "no_reason",
                            signal.score,
                            signal.regime,
                            signal.bias,
                            format_reasons(signal.reasons),
                        )
                        continue

                    assert signal.entry is not None
                    assert signal.stop_loss is not None
                    assert signal.take_profit is not None

                    price = signal.entry
                    ema_fast = closed_df["close"].ewm(span=settings.ema_fast).mean().iloc[-1]

                    distance_pct = abs(price - ema_fast) / price * 100

                    if distance_pct > 0.35:
                        logger.info("%s | No entry: too_far_from_ema | distance=%.3f%%", symbol, distance_pct)
                        continue

                    if has_unclosed_position_in_trades(symbol):
                        logger.warning("%s | Duplicate entry blocked: unclosed paper_open exists in trades.csv", symbol)
                        open_positions = load_open_positions(symbols)
                        save_open_positions(open_positions)
                        continue

                    qty, notional = risk.position_size(signal.entry, signal.stop_loss)

                    qty, notional, cap_reason = cap_position_size_by_capital(
                        qty=float(qty),
                        notional=float(notional),
                        entry=float(signal.entry),
                        open_positions=open_positions,
                        balance=float(risk.state.balance),
                    )

                    if qty <= 0 or notional <= 0:
                        logger.info("%s | No entry: capital_guard_no_available_capital", symbol)
                        if "market_snapshots" in locals():
                            market_snapshots.setdefault(symbol, {}).update({
                                "signal": "blocked",
                                "reason": "capital_guard_no_available_capital",
                            })
                        continue

                    capital_ok, capital_reason, capital_status = can_open_position_by_capital(
                        open_positions=open_positions,
                        balance=float(risk.state.balance),
                        proposed_notional=float(notional),
                    )

                    if not capital_ok:
                        logger.info(
                            "%s | No entry: %s | proposed_notional=%.2f current_notional=%.2f "
                            "used_margin=%.2f free_margin=%.2f balance=%.2f",
                            symbol,
                            capital_reason,
                            float(notional),
                            capital_status["current_notional"],
                            capital_status["used_margin"],
                            capital_status["free_margin"],
                            float(risk.state.balance),
                        )
                        if "market_snapshots" in locals():
                            market_snapshots.setdefault(symbol, {}).update({
                                "signal": "blocked",
                                "reason": capital_reason,
                            })
                        continue

                    if cap_reason != "unchanged":
                        logger.info(
                            "%s | Position size adjusted by capital guard | reason=%s qty=%.8f notional=%.2f",
                            symbol,
                            cap_reason,
                            float(qty),
                            float(notional),
                        )

                    open_positions[symbol] = {
                        "time": utc_now(),
                        "symbol": symbol,
                        "side": signal.side,
                        "entry": float(signal.entry),
                        "stop_loss": float(signal.stop_loss),
                        "take_profit": float(signal.take_profit),
                        "qty": float(qty),
                        "notional": float(notional),
                        "score": float(signal.score),
                        "regime": signal.regime,
                        "bias": signal.bias,
                        "reason": format_reasons(signal.reasons),
                        "opened_at": utc_now(),
                        "initial_stop_loss": float(signal.stop_loss),
                        "initial_risk": abs(float(signal.entry) - float(signal.stop_loss)),
                        "break_even_moved": False,
                        "trailing_active": False,
                    }
                    save_open_positions(open_positions)
                    market_snapshots.setdefault(symbol, {}).update({
                        "signal": f"ENTRY {signal.side}",
                        "score": round(float(signal.score), 2),
                        "regime": signal.regime,
                        "bias": signal.bias,
                        "reason": format_reasons(signal.reasons),
                    })

                    entry_msg = (
                        f"PAPER ENTRY {signal.side} | {symbol} | "
                        f"entry={signal.entry:.4f} "
                        f"sl={signal.stop_loss:.4f} "
                        f"tp={signal.take_profit:.4f} "
                        f"qty={qty:.6f} "
                        f"notional={notional:.2f} "
                        f"score={signal.score:.2f} "
                        f"regime={signal.regime} "
                        f"reasons={format_reasons(signal.reasons)}"
                    )

                    logger.info(entry_msg)
                    tg_trades.send("🟢 " + entry_msg)

                    store.append(
                        {
                            "time": utc_now(),
                            "symbol": symbol,
                            "side": signal.side,
                            "entry": round(signal.entry, 6),
                            "exit": "",
                            "stop_loss": round(signal.stop_loss, 6),
                            "take_profit": round(signal.take_profit, 6),
                            "qty": round(qty, 8),
                            "notional": round(notional, 2),
                            "gross_pnl": "",
                            "fee": "",
                            "net_pnl": "",
                            "score": round(signal.score, 2),
                            "regime": signal.regime,
                            "bias": signal.bias,
                            "status": "paper_open",
                            "reason": format_reasons(signal.reasons),
                            "balance_after": round(risk.state.balance, 2),
                        }
                    )

                except Exception as symbol_exc:
                    logger.exception("%s | Symbol processing error: %s", symbol, symbol_exc)
                    tg_warnings.send(f"🔴 ERROR | {symbol} | {symbol_exc}")
                    time.sleep(3)
                    continue

            time.sleep(settings.poll_seconds)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            try:
                telegram_control_thread.stop()
            except Exception:
                pass
            tg.send("🛑 Bot stopped by user")
            break

        except Exception as exc:
            logger.exception("Bot error: %s", exc)
            tg_warnings.send(f"🔴 ERROR | Bot error: {exc}")

            time.sleep(10)
            continue


if __name__ == "__main__":
    main()
