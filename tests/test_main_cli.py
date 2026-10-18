"""main.py wiring for the team UCI points commands and init_db's UCI-points schema."""
import sqlite3
import types

import pytest

import main
from db.database import init_db

UCI_TABLES = {"uci_team_totals", "uci_team_breakdown", "rider_uci_results",
              "rider_uci_penalties", "rider_team_spells"}


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_init_db_creates_uci_points_tables(tmp_path):
    db = tmp_path / "c.db"
    init_db(str(db))
    init_db(str(db))  # idempotent
    assert UCI_TABLES <= _tables(db)
    assert {"results", "stages", "races", "riders"} <= _tables(db)


@pytest.mark.parametrize("cmd,script", [("scrape-uci-points", "scrape_uci_points"),
                                        ("team-points", "team_points_report")])
def test_passthrough_commands_forward_argv(monkeypatch, cmd, script):
    seen = {}

    def fake_load(name):
        seen["name"] = name
        return types.SimpleNamespace(run=lambda argv: seen.setdefault("argv", argv) and 0)

    monkeypatch.setattr(main, "_load_script", fake_load)
    main.main([cmd, "--season", "2026", "--help", "-v"])
    assert seen == {"name": script, "argv": ["--season", "2026", "--help", "-v"]}


def test_passthrough_nonzero_exit_code(monkeypatch):
    monkeypatch.setattr(main, "_load_script", lambda name: types.SimpleNamespace(run=lambda argv: 2))
    with pytest.raises(SystemExit) as exc:
        main.main(["scrape-uci-points", "--season", "2026"])
    assert exc.value.code == 2


def test_other_commands_still_reject_unknown_args():
    with pytest.raises(SystemExit) as exc:
        main.main(["init", "--bogus"])
    assert exc.value.code == 2


def test_load_script_imports_by_path():
    mod = main._load_script("team_points_report")
    assert callable(mod.run)
    mod = main._load_script("scrape_uci_points")
    assert callable(mod.run)


def test_team_points_end_to_end_missing_tables(tmp_path, capsys):
    """team-points on a DB without the UCI tables exits 1 and leaves the DB untouched."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    with pytest.raises(SystemExit) as exc:
        main.main(["team-points", "--db", str(db), "--season", "2026"])
    assert exc.value.code == 1
    assert "no uci_team_totals" in capsys.readouterr().err
    assert _tables(db) == set()
