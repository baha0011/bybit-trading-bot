from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import html
import requests
from dotenv import load_dotenv

load_dotenv()


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.token = token
        self.chat_id = str(chat_id).strip() if chat_id is not None else ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        raw_text = str(text)

        if raw_text.startswith("🟢"):
            title = "🟢 ENTRY / ВХОД"
        elif raw_text.startswith("✅"):
            title = "✅ EXIT / ЗАКРЫТИЕ"
        elif raw_text.startswith("🔴 LOSS"):
            title = "🔴 LOSS / УБЫТОК"
        elif raw_text.startswith("🔴 ERROR"):
            title = "🔴 ERROR / ОШИБКА"
        elif raw_text.startswith("🟡"):
            title = "🟡 BREAK-EVEN"
        elif raw_text.startswith("🛑"):
            title = "🛑 BOT STOPPED"
        elif raw_text.startswith("🤖"):
            title = "🤖 BOT STARTED"
        elif "No entry:" in raw_text:
            title = "🔎 NO ENTRY / СИГНАЛА НЕТ"
        elif "Cooldown active" in raw_text:
            title = "⏳ COOLDOWN"
        elif "Position open" in raw_text:
            title = "📊 POSITION OPEN"
        elif "Trading blocked" in raw_text:
            title = "🚫 RISK MANAGER"
        elif "MTF/OB" in raw_text:
            title = "📈 Multi-Timeframe / OrderBook"
        else:
            title = "📩 BOT LOG"

        clean_text = raw_text

        for prefix in ("🟢 ", "✅ ", "🔴 ", "🟡 ", "🛑 ", "🤖 "):
            if clean_text.startswith(prefix):
                clean_text = clean_text[len(prefix):]

        safe_title = html.escape(title)
        safe_text = html.escape(clean_text)

        message = (
            f"<b>{safe_title}</b>\n\n"
            f'<pre><code class="language-python">{safe_text}</code></pre>'
        )

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            requests.post(url, json=payload, timeout=5)
        except requests.RequestException:
            pass

    def send_raw(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        """
        Отправка обычного HTML-сообщения без автоматического заголовка.
        Удобно для меню и ответов на кнопки.
        """
        if not self.enabled:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": str(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            requests.post(url, json=payload, timeout=5)
        except requests.RequestException:
            pass

    def send_document(self, file_path: str | Path, caption: str = "") -> None:
        if not self.enabled:
            return

        path = Path(file_path)

        if not path.exists():
            self.send_raw(f"⚠️ Файл не найден:\n<code>{html.escape(str(path))}</code>")
            return

        url = f"https://api.telegram.org/bot{self.token}/sendDocument"

        try:
            with path.open("rb") as file:
                requests.post(
                    url,
                    data={
                        "chat_id": self.chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    files={"document": (path.name, file)},
                    timeout=20,
                )
        except requests.RequestException:
            pass

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        if not self.token:
            return []

        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }

        if offset is not None:
            payload["offset"] = offset

        try:
            response = requests.get(url, params=payload, timeout=timeout + 6)
            data = response.json()
        except Exception:
            return []

        if not data.get("ok"):
            return []

        result = data.get("result", [])
        return result if isinstance(result, list) else []

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        if not self.token or not callback_query_id:
            return

        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"

        try:
            requests.post(
                url,
                json={
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": False,
                },
                timeout=5,
            )
        except requests.RequestException:
            pass

    def set_commands(self) -> None:
        if not self.token:
            return

        url = f"https://api.telegram.org/bot{self.token}/setMyCommands"
        commands = [
            {"command": "start", "description": "Открыть меню управления"},
            {"command": "status", "description": "Статус бота"},
            {"command": "positions", "description": "Открытые позиции"},
            {"command": "balance", "description": "Баланс и PnL"},
            {"command": "trades", "description": "Последние сделки"},
            {"command": "file", "description": "Выгрузить trades.csv"},
            {"command": "pause", "description": "Поставить паузу на новые входы"},
            {"command": "resume", "description": "Возобновить входы"},
        ]

        try:
            requests.post(url, json={"commands": commands}, timeout=5)
        except requests.RequestException:
            pass


class TelegramLogHandler(logging.Handler):
    def __init__(self, notifier: TelegramNotifier) -> None:
        super().__init__()
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "_sent_to_telegram", False):
                return

            setattr(record, "_sent_to_telegram", True)

            msg = self.format(record)

            if len(msg) > 3900:
                msg = msg[:3900] + "\n\n...сообщение обрезано"

            self.notifier.send(msg)

        except Exception as e:
            print("TelegramLogHandler error:", e)


def setup_telegram_logging(logger: logging.Logger | None = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    logs_chat_id = os.getenv("TELEGRAM_CHAT_ID_LOGS", "").strip()
    warnings_chat_id = os.getenv("TELEGRAM_CHAT_ID_WARNINGS", "").strip()

    if not token:
        print("Telegram logging disabled: TELEGRAM_BOT_TOKEN is empty")
        return

    if not logs_chat_id:
        print("Telegram logging disabled: TELEGRAM_CHAT_ID_LOGS is empty")
        return

    logs_notifier = TelegramNotifier(token=token, chat_id=logs_chat_id)
    warnings_notifier = TelegramNotifier(token=token, chat_id=warnings_chat_id)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s | %(filename)s:%(lineno)d"
    )

    class ExactInfoFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return record.levelno < logging.WARNING

    class WarningAndAboveFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return record.levelno >= logging.WARNING

    def add_handlers(target_logger: logging.Logger) -> None:
        target_logger.handlers = [
            h for h in target_logger.handlers
            if not isinstance(h, TelegramLogHandler)
        ]

        logs_handler = TelegramLogHandler(logs_notifier)
        logs_handler.setLevel(logging.INFO)
        logs_handler.setFormatter(formatter)
        logs_handler.addFilter(ExactInfoFilter())
        target_logger.addHandler(logs_handler)

        if warnings_notifier.enabled:
            warnings_handler = TelegramLogHandler(warnings_notifier)
            warnings_handler.setLevel(logging.WARNING)
            warnings_handler.setFormatter(formatter)
            warnings_handler.addFilter(WarningAndAboveFilter())
            target_logger.addHandler(warnings_handler)

        target_logger.setLevel(logging.INFO)

    add_handlers(logging.getLogger())

    if logger is not None:
        add_handlers(logger)
        logger.propagate = False

    print("Telegram logging enabled")
