"""Consistent online SQLite backups, including committed WAL contents.

Run from the project root: python -m bot.backup --output backups/bot.sqlite3
This command is independent of the Telegram token and the running bot process.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
from collections.abc import Sequence


def backup_database(database: str | Path, output: str | Path) -> Path:
    """Create a checked backup without replacing an existing destination.

    Opening the source in read-only URI mode also prevents SQLite from silently
    creating a new, empty source database if the supplied path is incorrect.
    """
    source = Path(database).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Путь исходной базы должен указывать на файл.")
    destination = Path(output).expanduser().resolve()
    if source == destination:
        raise ValueError("Резервная копия должна отличаться от исходной базы.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Reserve the destination atomically; a prior backup must never be replaced.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy, pages=100, sleep=0.1)
                results = copy.execute("PRAGMA quick_check").fetchall()
                if results != [("ok",)]:
                    raise sqlite3.DatabaseError("Проверка резервной копии завершилась ошибкой.")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Безопасная резервная копия SQLite без остановки бота.")
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
        help="Исходная SQLite (по умолчанию DATABASE_PATH из окружения/.env).",
    )
    parser.add_argument("--output", required=True, help="Новый файл резервной копии; существующий не перезаписывается.")
    args = parser.parse_args(argv)
    try:
        destination = backup_database(args.database, args.output)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Не удалось создать резервную копию: {error}", file=sys.stderr)
        return 1
    print(f"Резервная копия создана: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
