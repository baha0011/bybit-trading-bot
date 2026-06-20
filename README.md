# Bybit Trading Bot MVP v1

Безопасный MVP торгового бота под Bybit.

## Что уже есть

- Paper trading на реальных рыночных данных Bybit.
- Получение свечей через Bybit API V5.
- Получение тикера bid/ask/last price.
- Анализ EMA, RSI, ATR, объёма, trend/range.
- Scoring-модель для входа.
- Risk manager: риск на сделку, дневной лимит, серия лоссов, максимум сделок.
- Автоматический расчёт SL / TP / размера позиции.
- CSV-журнал виртуальных сделок.
- Подробные логи в консоли и файле.
- Telegram-уведомления, если прописать токен и chat_id.

## Важно

По умолчанию бот НЕ торгует реальными деньгами. Режим `paper` только симулирует сделки.

Live-режим в этой версии намеренно заблокирован. Сначала нужно проверить стратегию в paper/testnet.

## Установка

```bash
cd bybit_trading_bot_mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## Настройка

Открой `.env` и поменяй при необходимости:

```env
SYMBOL=ETHUSDT
TIMEFRAME=5
INITIAL_BALANCE_USDT=10000
RISK_PER_TRADE_PCT=1
MIN_SCORE_TO_ENTER=7
```

## Логи

Логи пишутся в:

```text
logs/bot.log
```

Сделки пишутся в:

```text
storage/trades.csv
```

## Как работает стратегия

Бот ищет сделку только когда совпадает несколько условий:

- цена относительно EMA;
- EMA fast/slow/trend;
- RSI не в экстремальной зоне;
- объём выше среднего;
- ATR позволяет поставить нормальный стоп;
- spread не слишком широкий;
- risk/reward не ниже заданного минимума;
- score выше порога.

## Следующие версии

v2:
- backtest engine;
- testnet real orders;
- улучшенная market structure: swing high/low, BOS/CHoCH;
- orderbook imbalance;
- web dashboard.
