import pytest

from scripts import scale_test


def test_populate_refuses_without_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        scale_test.populate(1)
    assert "DATABASE_URL" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_describe_target_masks_the_password():
    shown = scale_test.describe_target("postgresql+psycopg://pm:secret@localhost:5433/pm")
    assert "secret" not in shown
    assert "localhost:5433/pm" in shown
    assert "demo.db" in scale_test.describe_target("sqlite:///demo.db")
