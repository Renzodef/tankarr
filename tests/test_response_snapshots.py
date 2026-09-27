import json

import pytest

from tankarr.response_snapshots import ResponseSnapshots


def snapshot(tmp_path, scope="library"):
    database = tmp_path / "database"
    database.touch(exist_ok=True)
    return ResponseSnapshots(tmp_path / "cache", database, scope=scope)


def test_snapshot_survives_restart_and_rejects_different_scope(tmp_path):
    snapshot(tmp_path).save("wanted", {"payload": b'[ {"id": "one"} ]'})
    assert snapshot(tmp_path).load("wanted") == {"payload": b'[ {"id": "one"} ]'}
    assert snapshot(tmp_path, scope="other-library").load("wanted") == {}
    (tmp_path / "database").rename(tmp_path / "old-database")
    assert snapshot(tmp_path).load("wanted") == {}


@pytest.mark.parametrize("change", ["version", "expired", "payload", "truncated"])
def test_invalid_snapshot_is_disposable(tmp_path, change):
    store = snapshot(tmp_path)
    store.save("wanted", {"payload": b"[]"})
    path = store.root / "wanted.json"
    saved = json.loads(path.read_text())
    if change == "version":
        saved["version"] = -1
    elif change == "expired":
        saved["created"] = 0
    elif change == "payload":
        saved["payloads"]["payload"] = "{}"
    path.write_text("{" if change == "truncated" else json.dumps(saved))
    assert store.load("wanted") == {}


def test_unwritable_cache_does_not_break_response(tmp_path):
    store = snapshot(tmp_path)
    store.root.write_text("not a directory")
    store.save("wanted", {"payload": b"[]"})
    assert store.load("wanted") == {}
