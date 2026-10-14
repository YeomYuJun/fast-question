"""검색 메모리 사본의 상한: 가장 오래 쓰지 않은 DB 부터 비운다(원격 모드는 사용자마다 DB 가 다르다)."""
import pytest

from kb import search
from kb.db import connect
from kb.store import Store


@pytest.fixture
def dbs(tmp_path):
    out = []
    for n in range(3):
        s = Store(connect(tmp_path / f"user{n}.sqlite3"), "user")
        s.add_category("이관")
        for i in range(n + 1):
            s.add_qa("이관", f"{n}번 DB 질문 {i}", "답이다.", force=True)
        out.append(s)
    yield out
    for s in out:
        s.conn.close()


def cached():
    return {k.rsplit("user", 1)[-1] for k in search._CACHE}


def test_least_recently_used_db_is_dropped(dbs, monkeypatch):
    monkeypatch.setattr(search, "_CACHE", type(search._CACHE)())
    monkeypatch.setattr(search, "CACHE_MAX_DBS", 2)
    dbs[0].search("질문")
    dbs[1].search("질문")
    dbs[0].search("질문")  # 0 을 다시 써서 1 이 가장 오래됐다
    dbs[2].search("질문")
    assert cached() == {"0.sqlite3", "2.sqlite3"}
    assert [h["question"] for h in dbs[1].search("질문")["hits"]] == ["1번 DB 질문 1", "1번 DB 질문 0"]  # 밀려나도 다시 읽는다


def test_total_docs_limit(dbs, monkeypatch):
    monkeypatch.setattr(search, "_CACHE", type(search._CACHE)())
    monkeypatch.setattr(search, "CACHE_MAX_DOCS", 4)
    for s in dbs:  # 항목 1, 2, 3건
        s.search("질문")
    assert cached() == {"2.sqlite3"}  # 3건 + 앞의 것을 더하면 4건을 넘는다
