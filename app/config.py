from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, '') else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, '') else float(raw)


@dataclass(frozen=True)
class Settings:
    bot_mode: str = _str('BOT_MODE', 'paper')
    bybit_env: str = _str('BYBIT_ENV', 'mainnet')
    category: str = _str('BYBIT_CATEGORY', 'linear')
    symbol: str = _str('SYMBOL', 'ETHUSDT')
    timeframe: str = _str('TIMEFRAME', '5')
    candle_limit: int = _int('CANDLE_LIMIT', 300)
    poll_seconds: int = _int('POLL_SECONDS', 60)

    initial_balance: float = _float('INITIAL_BALANCE_USDT', 10_000.0)
    risk_per_trade_pct: float = _float('RISK_PER_TRADE_PCT', 1.0)
    max_daily_loss_pct: float = _float('MAX_DAILY_LOSS_PCT', 3.0)
    max_trades_per_day: int = _int('MAX_TRADES_PER_DAY', 5)
    max_losing_streak: int = _int('MAX_LOSING_STREAK', 3)
    min_rr: float = _float('MIN_RR', 2.0)
    max_spread_pct: float = _float('MAX_SPREAD_PCT', 0.08)
    fee_rate: float = _float('FEE_RATE', 0.0006)
    slippage_pct: float = _float('SLIPPAGE_PCT', 0.03)

    ema_fast: int = _int('EMA_FAST', 20)
    ema_slow: int = _int('EMA_SLOW', 50)
    ema_trend: int = _int('EMA_TREND', 200)
    rsi_period: int = _int('RSI_PERIOD', 14)
    atr_period: int = _int('ATR_PERIOD', 14)
    volume_ma_period: int = _int('VOLUME_MA_PERIOD', 20)
    min_score_to_enter: float = _float('MIN_SCORE_TO_ENTER', 7.0)

    bybit_api_key: str = _str('BYBIT_API_KEY', '')
    bybit_api_secret: str = _str('BYBIT_API_SECRET', '')
    telegram_bot_token: str = _str('TELEGRAM_BOT_TOKEN', '')
    telegram_chat_id: str = _str('TELEGRAM_CHAT_ID', '')

    @property
    def testnet(self) -> bool:
        return self.bybit_env.lower() == 'testnet'

    def validate(self) -> None:
        if self.bot_mode not in {'paper', 'testnet', 'live'}:
            raise ValueError('BOT_MODE must be paper, testnet, or live')
        if self.bot_mode == 'live':
            raise ValueError('Live mode is blocked in MVP v1. Use paper first.')
        if self.initial_balance <= 0:
            raise ValueError('INITIAL_BALANCE_USDT must be positive')
        if not 0 < self.risk_per_trade_pct <= 5:
            raise ValueError('RISK_PER_TRADE_PCT must be between 0 and 5')


settings = Settings()
settings.validate()
