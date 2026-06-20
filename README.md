# BhTrade Bybit Trading Bot

Локальный Python-бот для paper-тестирования торговой стратегии на Bybit с Telegram-управлением, multi-symbol анализом, MTF/Orderbook фильтрами и общим контролем капитала.

> Текущий статус: **paper trading only**. Live-режим не использовать, пока стратегия не соберёт стабильную статистику на paper.

---

## Что это за проект

Бот анализирует рынок Bybit, ищет торговые сетапы и симулирует сделки в paper-режиме.

Рабочая схема:

```text
bot.py
  ↓
Bybit market data
  ↓
Strategy scoring
  ↓
Risk manager + Capital guard
  ↓
Paper position
  ↓
storage/trades.csv
  ↓
Telegram notifications / control
```

Веб-dashboard больше не используется. Управление идёт через терминал и Telegram.

---

## Основные возможности

### Market data

- Bybit API V5
- свечи;
- ticker bid/ask/last;
- orderbook;
- несколько символов одновременно.

### Символы

По умолчанию:

```text
BTCUSDT, ETHUSDT, SOLUSDT
```

### Стратегия

Бот учитывает:

- EMA stack;
- EMA slope;
- RSI;
- позицию цены в диапазоне;
- объём выше среднего;
- trend / range режим;
- liquidity sweep;
- imbalance 50% reaction;
- MTF-контекст;
- orderbook pressure;
- spread;
- risk/reward;
- итоговый score.

### Управление позицией

- Stop Loss;
- Take Profit;
- break-even;
- trailing stop;
- time stop;
- invalidated setup;
- расчёт комиссий;
- учёт net PnL.

### Risk management

- риск на сделку;
- дневной лимит убытка;
- лимит сделок в день;
- лимит серии убыточных сделок;
- общий paper-баланс;
- восстановление баланса после перезапуска.

### Capital Guard

Баланс общий для всех монет.

Если баланс `100 USDT`, это не значит `100 USDT` на BTC, `100 USDT` на ETH и `100 USDT` на SOL.

Защиты:

```env
MAX_LEVERAGE=2
MAX_OPEN_POSITIONS=1
MAX_TOTAL_EXPOSURE_PCT=150
MAX_POSITION_NOTIONAL_PCT=120
MAX_MARGIN_USAGE_PCT=80
```

---

## Структура проекта

```text
bybit-trading-bot/
│
├── bot.py
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
│
├── app/
│   ├── config.py
│   └── logger.py
│
├── exchange/
│   └── bybit_client.py
│
├── strategy/
│   ├── indicators.py
│   └── scoring_strategy.py
│
├── analysis/
│   ├── market_context.py
│   └── orderbook_analyzer.py
│
├── risk/
│   └── risk_manager.py
│
├── storage/
│   ├── trade_store.py
│   └── trades.csv
│
├── notifications/
│   └── telegram_notifier.py
│
├── logs/
│   └── bot.log
│
└── state/
    ├── open_positions.json
    ├── telegram_control_flags.json
    └── telegram_control_state.json
```

Файлы `logs/`, `state/`, `storage/trades.csv` и `.env` не должны попадать в GitHub.

---

## Установка

```bash
cd /Users/job/Desktop/bybit_trading_bot_mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

---

## Настройка `.env`

Пример безопасной конфигурации для paper-теста на `100 USDT`:

```env
BOT_MODE=paper
BYBIT_ENV=mainnet

SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
TIMEFRAME=5
CANDLE_LIMIT=300

INITIAL_BALANCE_USDT=100
RISK_PER_TRADE_PCT=0.5
MAX_DAILY_LOSS_PCT=3
MAX_TRADES_PER_DAY=5
MAX_LOSING_STREAK=2

MAX_LEVERAGE=2
MAX_OPEN_POSITIONS=1
MAX_TOTAL_EXPOSURE_PCT=150
MAX_POSITION_NOTIONAL_PCT=120
MAX_MARGIN_USAGE_PCT=80

TELEGRAM_BOT_TOKEN=PUT_YOUR_TOKEN_HERE
TELEGRAM_CHAT_ID_WARNINGS=PUT_WARNINGS_CHAT_ID
TELEGRAM_CHAT_ID_LOGS=PUT_LOGS_CHAT_ID
TELEGRAM_CHAT_ID_TRADES=PUT_TRADES_CHAT_ID
TELEGRAM_CHAT_ID_PROFIT=PUT_PROFIT_CHAT_ID
TELEGRAM_CHAT_ID_LOSS=PUT_LOSS_CHAT_ID
TELEGRAM_CHAT_ID_CONTROL=PUT_CONTROL_CHAT_ID

MIN_SCORE_TO_ENTER=4.5
MIN_RR=1.7
MAX_SPREAD_PCT=0.08
POLL_SECONDS=60
FEE_RATE=0.0006
SLIPPAGE_PCT=0.03

TIMEFRAMES=1,5,15,30,60,240,D,W
ORDERBOOK_LIMIT=50
USE_MTF_FILTER=true
USE_ORDERBOOK_FILTER=true
```

---

## Запуск

Перед запуском убедись, что старые процессы остановлены:

```bash
ps -axww -o pid,ppid,lstart,command | grep -Ei "python|Python|bybit|bot"
```

Остановить всё, что относится к проекту:

```bash
pkill -9 -f "bybit_trading_bot_mvp"
pkill -9 -f "bot_with"
pkill -9 -f "bot_final"
pkill -9 -f "bot_fast"
```

Запуск:

```bash
python bot.py
```

Остановка:

```text
Ctrl + C
```

---

## Проверка, что работает один бот

Проверить, кто пишет в лог:

```bash
lsof | grep bot.log
```

Норма — одна строка:

```text
Python  12345  job  ... /logs/bot.log
```

Если строк несколько, значит запущено несколько экземпляров бота. Это опасно: они будут путать позиции, лог и Telegram.

---

## Чистый старт

```bash
pkill -9 -f "bybit_trading_bot_mvp"

rm -f ~/.bhtrade_bybit_bot.lock
rm -f state/open_positions.json
rm -f state/telegram_control_flags.json
rm -f state/telegram_control_state.json

> logs/bot.log
```

Очистить CSV сделок:

```bash
cat > storage/trades.csv << 'EOF'
time,symbol,side,entry,stop_loss,take_profit,qty,notional,score,regime,bias,status,reason,balance_after
EOF
```

Запуск:

```bash
python bot.py
```

---

## Telegram управление

Доступные команды:

```text
/start
/status
/balance
/positions
/trades
/file
/pause
/resume
/emergency
/market
```

Кнопки:

```text
📊 Статус
💰 Баланс / PnL
📈 Позиции
🧾 Последние сделки
🧠 Что видит бот
📁 Выгрузить trades.csv
⏸ Пауза входов
▶️ Возобновить
⚠️ Emergency stop
🔄 Обновить меню
```

---

## Логи и сделки

Логи:

```text
logs/bot.log
```

Сделки:

```text
storage/trades.csv
```

Открытые позиции:

```text
state/open_positions.json
```

---

## Как понять, что всё работает правильно

Хорошие признаки:

```text
balance=100.00
notional около 80-120
одна открытая позиция максимум
один процесс пишет в bot.log
нет дублей по одному символу
```

Плохие признаки:

```text
balance_after=10000
notional=19000+
несколько Python-процессов пишут в bot.log
один символ открыт несколько раз одновременно
```

---

## Почему бот может не входить

Это нормально:

```text
No entry: score_too_low
No entry: range_requires_liquidity_AND_imbalance
No entry: orderbook_against_long
No entry: orderbook_against_short
No entry: too_far_from_ema
```

Бот не должен входить постоянно. Задача — не количество сделок, а качество входов.

---

## Перед live

Перед реальными деньгами нужно:

```text
1. Прогнать paper на 100 USDT
2. Собрать минимум 30-50 закрытых сделок
3. Убедиться, что нет дублей
4. Убедиться, что один процесс пишет в bot.log
5. Проверить, что notional не улетает в 19000+
6. Проверить, что баланс идёт одной линией от 100 USDT
7. Проверить Profit Factor, winrate и max drawdown
```

Рекомендуемый первый live-риск:

```env
RISK_PER_TRADE_PCT=0.3
MAX_OPEN_POSITIONS=1
MAX_LEVERAGE=1
MAX_TRADES_PER_DAY=3
MAX_LOSING_STREAK=2
```

---

## Безопасность

Нельзя загружать в GitHub:

```text
.env
logs/
state/
storage/trades.csv
Telegram token
API keys
```

Если Telegram token был опубликован — перевыпустить через BotFather:

```text
/revoke
```

---

## Git

Проверить статус:

```bash
git status
```

Проверить, что секреты не попали в Git:

```bash
git ls-files | grep -E ".env|bot.log|trades.csv|open_positions"
```

Если команда что-то вывела — нельзя пушить, нужно удалить из индекса.

Коммит:

```bash
git add .
git commit -m "Update README and project config"
git push
```

Создать стабильную точку:

```bash
git tag stable-paper-100-v1
git push origin stable-paper-100-v1
```

---

## Важное предупреждение

Проект экспериментальный. Paper trading не гарантирует прибыль в live.

Риски:

```text
резкие движения рынка
проскальзывание
комиссии
ошибки API
сбой интернета
ошибки стратегии
несколько процессов одновременно
```

Не использовать деньги, которые нельзя потерять.
