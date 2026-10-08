"""화면 확인용 임시 DB 와 config 를 만든다. 실제 DB(data/kb.sqlite3)는 건드리지 않는다.

화면의 상태가 모두 나오도록 넣는다: 사용자·Claude 작성, 근거 없음·외부 근거·바뀐 근거(재확인),
관련 질문 연결, 글자 수 한도(150/400자), 이모지가 든 강조, Claude 가 고친 이력.
사용: .venv\\Scripts\\python.exe scripts\\demo_db.py [폴더] [--port 8766]
      .venv\\Scripts\\python.exe run_web.py --config <출력된 config.json>
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kb.db import connect  # noqa: E402
from kb.store import Store  # noqa: E402
from tests.conftest import load_sample  # noqa: E402

DEFAULT_DIR = Path(tempfile.gettempdir()) / "fast-question-demo"


def build(folder: Path, port: int = 8766, analyzer=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("kb.sqlite3*"):
        old.unlink()
    conn = connect(folder / "kb.sqlite3")
    try:
        user, claude = Store(conn, "user", analyzer), Store(conn, "claude", analyzer)
        load_sample(user)
        ev = folder / "근거 폴더" / "NasFileStorage.java"
        ev.parent.mkdir(exist_ok=True)
        ev.write_text('class Nas { String stage = ".nas-tmp"; }\n', encoding="utf-8")
        cls = claude.add_qa("이관 > NAS", ".nas-tmp 를 쓰는 클래스는?", "NasFileStorage 가 .nas-tmp 아래에 임시 파일을 쓴다.",
                            evidence=[{"kind": "auto", "path": str(ev), "anchor": "stage"}], force=True)["item"]
        claude.add_qa("일반 지식 > k8s", "ConfigMap 을 바꿨는데 반영이 안 되는 이유는?",
                      "환경변수로 주입된 값은 Pod 가 재시작되기 전까지 갱신되지 않는다.",
                      evidence=[{"kind": "url", "url": "https://kubernetes.io/docs/concepts/configuration/configmap/"}])
        claude.add_qa("일반 지식 > k8s", "이모지 😀 가 든 질문에서 rename 강조가 맞는가?",
                      "😀😀 rename 위치가 밀리지 않아야 한다.", force=True)
        claude.add_qa("이관 > 네트워크", "가" * 150, "나" * 199 + "\n" + "다" * 200, force=True)
        claude.add_qa("이관 > DB 암호화", "PETRA 제거는 누가 결정했나?", "고객사 DBA 가 회의에서 정했다.",
                      evidence=[{"kind": "note", "text": "사용자가 회의록으로 확인"}], force=True)
        user.link(1, 2)
        user.link(1, cls["id"])
        q = claude.get_qa(5)
        claude.update_qa(5, q["version"], answer=q["answer"] + " (Claude 가 고침)", note="표현 정리")
        ev.write_text('class Nas { String stage = ".nas-tmp2"; }\n', encoding="utf-8")  # 근거가 바뀌어 재확인이 된다
        Store(conn, "system").recheck()
    finally:
        conn.close()
    cfg = folder / "config.json"
    cfg.write_text(json.dumps({"db_path": str(folder / "kb.sqlite3"), "port": port,
                               "backup_dir": str(folder / "backup"), "log_dir": str(folder / "logs")},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    from kb.morph import Analyzer
    print(build(args.folder.resolve(), args.port, Analyzer()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
