import codecs
import json

from kb.config import ROOT, load_config


def test_reads_bom_json_and_resolves_relative_paths(tmp_path):
    p = tmp_path / "config.json"
    body = json.dumps({"db_path": "x/kb.sqlite3", "port": 9999}, ensure_ascii=False)
    p.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))

    cfg = load_config(p)

    assert cfg.port == 9999
    assert cfg.db_path == ROOT / "x" / "kb.sqlite3"


def test_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "없음.json")

    assert cfg.port == 8765
    assert cfg.host == "127.0.0.1"
    assert cfg.db_path == ROOT / "data" / "kb.sqlite3"
    assert cfg.backup_keep == 14


def test_absolute_path_kept(tmp_path):
    p = tmp_path / "config.json"
    target = tmp_path / "한글 폴더" / "kb.sqlite3"
    p.write_text(json.dumps({"db_path": str(target)}, ensure_ascii=False), encoding="utf-8")

    assert load_config(p).db_path == target
