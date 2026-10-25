import json

from kb.db import connect


def test_demo_db_has_every_screen_state(tmp_path):
    from scripts.demo_db import build

    cfg_path = build(tmp_path / "데모 폴더", port=8799)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["port"] == 8799
    conn = connect(cfg["db_path"])
    states = {r[0] for r in conn.execute("SELECT evidence_state FROM qa")}
    assert {"none", "external", "stale"} <= states  # 근거가 바뀐 항목이 있다
    authors = {r[0] for r in conn.execute("SELECT created_by FROM qa")}
    assert authors == {"user", "claude"}
    assert conn.execute("SELECT count(*) FROM qa_link").fetchone()[0] >= 1
    assert conn.execute("SELECT max(length(question)) FROM qa").fetchone()[0] == 150
    # 예시는 캔버스 Main 보드와 같은 웹 개발 내용이다
    assert conn.execute("SELECT question FROM qa WHERE id = 1").fetchone()[0] == "API 를 부를 때 CORS 오류가 나는 이유는?"
    conn.close()
    assert build(tmp_path / "데모 폴더", port=8799) == cfg_path  # 다시 만들어도 된다
