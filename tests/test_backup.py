from pathlib import Path
import sqlite3

import pytest

from bot.backup import backup_database, main


def test_online_backup_includes_committed_wal_and_keeps_source_usable(tmp_path: Path) -> None:
    source = tmp_path / "bot.sqlite3"
    output = tmp_path / "backups" / "copy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE tasks (title TEXT NOT NULL)")
        connection.execute("INSERT INTO tasks VALUES (?)", ("Принять витамины",))
        connection.commit()
        assert Path(f"{source}-wal").stat().st_size > 0

        assert backup_database(source, output) == output.resolve()
        connection.execute("INSERT INTO tasks VALUES (?)", ("После копирования",))
        connection.commit()

    with sqlite3.connect(output) as copy:
        assert copy.execute("SELECT title FROM tasks").fetchall() == [("Принять витамины",)]
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone() == (2,)


def test_backup_never_overwrites_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    sqlite3.connect(source).close()
    output = tmp_path / "existing.sqlite3"
    output.write_bytes(b"original backup")

    with pytest.raises(FileExistsError):
        backup_database(source, output)

    assert output.read_bytes() == b"original backup"


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite3"
    output = tmp_path / "copy.sqlite3"

    with pytest.raises(FileNotFoundError):
        backup_database(source, output)

    assert not source.exists()
    assert not output.exists()


def test_backup_rejects_source_as_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE tasks (id INTEGER)")

    with pytest.raises(ValueError):
        backup_database(source, source)

    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_invalid_database_cleans_partial_backup(tmp_path: Path) -> None:
    source = tmp_path / "invalid.sqlite3"
    source.write_bytes(b"not a SQLite database")
    output = tmp_path / "copy.sqlite3"

    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, output)

    assert source.read_bytes() == b"not a SQLite database"
    assert not output.exists()


def test_backup_cli_needs_no_bot_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    source = tmp_path / "source.sqlite3"
    sqlite3.connect(source).close()
    output = tmp_path / "copy.sqlite3"

    assert main(["--database", str(source), "--output", str(output)]) == 0
    assert output.is_file()
    assert "Резервная копия создана" in capsys.readouterr().out


def test_backup_cli_returns_error_for_missing_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--database", str(tmp_path / "absent.sqlite3"), "--output", str(tmp_path / "copy.sqlite3")]) == 1
    assert "Не удалось создать резервную копию" in capsys.readouterr().err
