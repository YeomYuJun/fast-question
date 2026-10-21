import codecs
import json

import pytest

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


def _write(tmp_path, body: dict):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return p


def test_mode_defaults_to_local(tmp_path):
    cfg = load_config(tmp_path / "없음.json")

    assert cfg.mode == "local"
    assert cfg.public_url == ""
    assert cfg.data_dir is None


def test_remote_mode_reads_url_and_data_dir(tmp_path):
    cfg = load_config(_write(tmp_path, {"mode": "remote", "public_url": "https://fq.example.com",
                                        "data_dir": "srv/한글 데이터"}))

    assert cfg.mode == "remote"
    assert cfg.public_url == "https://fq.example.com"
    assert cfg.data_dir == ROOT / "srv" / "한글 데이터"


@pytest.mark.parametrize("body, needle", [
    ({"mode": "cloud"}, "mode"),
    ({"mode": "remote", "data_dir": "d"}, "public_url"),
    ({"mode": "remote", "public_url": "fq.example.com", "data_dir": "d"}, "public_url"),
    ({"mode": "remote", "public_url": "https://fq.example.com/kb", "data_dir": "d"}, "public_url"),
    ({"mode": "remote", "public_url": "https://fq.example.com"}, "data_dir"),
])
def test_remote_mode_requires_valid_settings(tmp_path, body, needle):
    with pytest.raises(ValueError, match=needle):
        load_config(_write(tmp_path, body))


def test_limits_and_corpus_cache_keys_override_defaults(tmp_path):
    from kb import config, search
    from web.limits import DEFAULT_LIMITS
    cfg = load_config(_write(tmp_path, {"limits": {"write": 30}, "corpus_cache": {"max_dbs": 50}}))
    assert cfg.limits == {"write": 30} and cfg.corpus_cache == {"max_dbs": 50}
    default = load_config(tmp_path / "없음.json")
    assert default.limits == {} and default.corpus_cache == {}
    assert set(config.LIMIT_KEYS) == set(DEFAULT_LIMITS)
    assert {k: getattr(search, f"CACHE_MAX_{k[4:].upper()}") for k in config.CORPUS_CACHE_KEYS} == {
        "max_dbs": search.CACHE_MAX_DBS, "max_docs": search.CACHE_MAX_DOCS}


@pytest.mark.parametrize("body", [{"limits": {"writes": 1}}, {"limits": {"write": 0}}, {"limits": {"write": True}},
                                  {"limits": [1]}, {"corpus_cache": {"max_dbs": "10"}}, {"corpus_cache": {"size": 1}}])
def test_bad_limit_keys_are_refused(tmp_path, body):
    with pytest.raises(ValueError, match="limits|corpus_cache"):
        load_config(_write(tmp_path, body))


def test_oauth_client_ids_and_allowed_logins(tmp_path):
    cfg = load_config(_write(tmp_path, {"mode": "remote", "public_url": "https://fq.example.com", "data_dir": "d",
                                        "oauth": {"github": {"client_id": "gh-id"}, "google": {"client_id": "g-id"}},
                                        "allowed_logins": ["github:42"]}))

    assert (cfg.github_client_id, cfg.google_client_id) == ("gh-id", "g-id")
    assert cfg.allowed_logins == frozenset({"github:42"})
    assert load_config(tmp_path / "없음.json").allowed_logins == frozenset()


def test_backup_s3_keys_default_empty_and_prefix_gets_slash(tmp_path):
    cfg = load_config(tmp_path / "없음.json")
    assert cfg.backup_s3_bucket == "" and cfg.backup_s3_prefix == ""

    cfg = load_config(_write(tmp_path, {"backup_s3_bucket": "fq-backup", "backup_s3_prefix": "prod"}))
    assert cfg.backup_s3_bucket == "fq-backup" and cfg.backup_s3_prefix == "prod/"
    assert load_config(_write(tmp_path, {"backup_s3_prefix": "prod/"})).backup_s3_prefix == "prod/"


def test_web_url_is_local_port_or_public_url(tmp_path):
    assert load_config(_write(tmp_path, {"port": 9999})).web_url == "http://127.0.0.1:9999"
    cfg = load_config(_write(tmp_path, {"mode": "remote", "public_url": "https://fq.example.com/", "data_dir": "d"}))
    assert cfg.web_url == "https://fq.example.com"
