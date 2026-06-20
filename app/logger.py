from __future__ import annotations

import logging
from pathlib import Path
from rich.logging import RichHandler

LOG_DIR = Path('logs')
LOG_DIR.mkdir(exist_ok=True)


def setup_logger(name: str = 'bot') -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')

    file_handler = logging.FileHandler(LOG_DIR / 'bot.log', encoding='utf-8')
    file_handler.setFormatter(formatter)

    console_handler = RichHandler(rich_tracebacks=True, markup=True)
    console_handler.setFormatter(logging.Formatter('%(message)s'))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
