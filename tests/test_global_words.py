"""원격 모드 형태소 사용자 단어는 제어 DB 의 서버 전역 사전 하나다. 분석기 버전도 그 사전으로 계산한다."""
import json

import pytest

from kb import control, tenant
from kb.config import Config
from kb.db import connect
from kb.morph import Analyzer, compute_version, pending_count
from kb.store import Store


class RecordingKiwi:
    loads = 0

    def __init__(self):
        type(self).loads += 1
        self.words = []

    def add_user_word(self, word, tag):
        self.words.append((word, tag))
        return True

    def tokenize(self, _):
        return []


@pytest.fixture(autouse=True)
def _reset():
    RecordingKiwi.loads = 0


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "local.sqlite3", host="0.0.0.0", port=8765, backup_dir=tmp_path / "backup",
                  backup_keep=3, log_dir=tmp_path / "logs", log_keep_days=14, recheck_interval_hours=24,
                  mode="remote", public_url="https://fq.example.com", data_dir=tmp_path / "data")


def control_conn(cfg):
    return control.connect_control(tenant.control_path(cfg.data_dir))


def user_db(cfg, account_id, local_word=None):
    conn = connect(tenant.db_path_for(cfg.data_dir, account_id))
    s = Store(conn, "user")
    if local_word:  # 로컬 DB 를 옮겨 오면 그 PC 의 사용자 단어가 따라온다
        s.dict_action("user_word_add", word=local_word, tag="NNP")
    s.add_category("이관")
    s.add_qa("이관", f"계정 {account_id} 질문", "답변이다.")
    return conn


def test_global_words_add_list_remove(cfg):
    conn = control_conn(cfg)
    control.add_global_word(conn, "망연계")
    control.add_global_word(conn, "fq", "SL")
    assert control.global_words(conn) == [("fq", "SL"), ("망연계", "NNP")]
    for word, tag in (("", "NNP"), ("두 단어", "NNP"), ("가" * 41, "NNP"), ("단어", "JKS")):
        with pytest.raises(ValueError):
            control.add_global_word(conn, word, tag)
    with pytest.raises(ValueError):
        control.add_global_word(conn, "망연계")  # 이미 있다
    assert control.remove_global_word(conn, "fq") and not control.remove_global_word(conn, "fq")
    assert control.global_words(conn) == [("망연계", "NNP")]
    conn.close()


def test_analyzer_uses_injected_words_not_db_words(cfg):
    conn = control_conn(cfg)
    control.add_global_word(conn, "망연계")
    conn.close()
    analyzer = Analyzer(loader=RecordingKiwi, words=control.words_source(tenant.control_path(cfg.data_dir)))
    a, b = user_db(cfg, 1, local_word="가나"), user_db(cfg, 2, local_word="다라")
    for _ in range(3):  # 사용자 DB 의 단어가 달라도 Kiwi 를 다시 띄우지 않는다
        assert analyzer.ready(a) and analyzer.ready(b)
    assert RecordingKiwi.loads == 1
    assert analyzer._kiwi.words == [("망연계", "NNP")]
    assert analyzer.version == compute_version([("망연계", "NNP")])
    assert pending_count(a, analyzer) == 1 and pending_count(a) == 1
    a.close()
    b.close()


def test_default_analyzer_keeps_local_db_words(tmp_path):
    conn = connect(tmp_path / "kb.sqlite3")
    Store(conn, "user").dict_action("user_word_add", word="망연계", tag="NNP")
    analyzer = Analyzer(loader=RecordingKiwi)
    assert analyzer.ready(conn) and analyzer._kiwi.words == [("망연계", "NNP")]
    conn.close()


def test_global_word_change_makes_every_user_db_reindex(cfg):
    from kb.jobs import run_due_all

    analyzer = Analyzer(loader=RecordingKiwi, words=control.words_source(tenant.control_path(cfg.data_dir)))
    for acc in (1, 2):
        user_db(cfg, acc).close()
    assert {acc: r["reindex"] for acc, r in run_due_all(cfg, analyzer)["users"].items()} == {1: 1, 2: 1}
    assert run_due_all(cfg, analyzer) == {}

    conn = control_conn(cfg)
    control.add_global_word(conn, "망연계")
    conn.close()
    done = run_due_all(cfg, analyzer)
    # 첫 주기 작업이 기본 가이드도 넣었으므로 DB 의 모든 항목이 다시 색인된다
    rows = {}
    for acc in (1, 2):
        c = connect(tenant.db_path_for(cfg.data_dir, acc))
        rows[acc] = c.execute("SELECT count(*) FROM qa").fetchone()[0]
        c.close()
    assert {acc: r["reindex"] for acc, r in done["users"].items()} == rows and rows[1] > 1
    assert RecordingKiwi.loads == 2  # 사전이 바뀌어 한 번만 다시 띄웠다


def test_remote_app_default_analyzer_reads_control_db(cfg):
    from web.app import create_app

    conn = control_conn(cfg)
    control.add_global_word(conn, "망연계")
    conn.close()
    app = create_app(cfg, run_jobs=False)  # Kiwi 는 처음 쓸 때 띄우므로 여기서는 뜨지 않는다
    user = user_db(cfg, 1, local_word="가나")
    assert app.state.analyzer.user_words(user) == [("망연계", "NNP")]
    user.close()


def test_global_words_cli(cfg, tmp_path, capsys):
    import scripts.global_words as gw

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mode": "remote", "public_url": cfg.public_url, "data_dir": str(cfg.data_dir)}),
                    encoding="utf-8")
    assert gw.main(["--config", str(path), "add", "망연계"]) == 0
    assert gw.main(["--config", str(path), "add", "fq", "SL"]) == 0
    assert gw.main(["--config", str(path), "list"]) == 0
    out = capsys.readouterr().out
    assert "망연계\tNNP" in out and "fq\tSL" in out
    assert gw.main(["--config", str(path), "add", "두 단어"]) == 1
    assert gw.main(["--config", str(path), "remove", "fq"]) == 0
    assert gw.main(["--config", str(path), "remove", "fq"]) == 1
    conn = control_conn(cfg)
    assert control.global_words(conn) == [("망연계", "NNP")]
    conn.close()
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"db_path": str(tmp_path / "kb.sqlite3")}), encoding="utf-8")
    assert gw.main(["--config", str(local), "list"]) == 2  # 로컬 모드 설정은 받지 않는다
