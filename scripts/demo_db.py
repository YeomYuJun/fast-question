"""화면 확인용 임시 DB 와 config 를 만든다. 실제 DB(data/kb.sqlite3)는 건드리지 않는다.

예시는 일반적인 웹 개발 내용이고 캔버스 Main 보드(design/screens/Main.dc.html)의 데이터와 같다.
화면의 상태가 모두 나오도록 넣는다: 사용자·Claude 작성, 근거 없음·외부 근거·바뀐 근거(재확인),
관련 질문 연결, 글자 수 한도(150/400자), 이모지가 든 강조, Claude 가 고친 이력.
테스트 표본(tests/conftest.py SAMPLE_QA)과는 따로 둔다. 검색 테스트가 그 내용에 기대기 때문이다.
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

DEFAULT_DIR = Path(tempfile.gettempdir()) / "fast-question-demo"

# 사용자가 쓴 예시 11건. id 는 넣는 순서대로 1~11 이 된다
DEMO_QA = [
    ("웹 > API", "API 를 부를 때 CORS 오류가 나는 이유는?",
     "서버 응답에 Access-Control-Allow-Origin 이 없어서다. 개발 중에는 프록시로 같은 출처를 만든다."),
    ("웹 > API", "PUT 과 PATCH 의 차이는?", "PUT 은 자원 전체를 바꾸고 PATCH 는 보낸 필드만 바꾼다."),
    ("웹 > API", "429 응답을 받으면 어떻게 하나?", "요청 한도를 넘은 것이다. Retry-After 만큼 기다렸다가 다시 보낸다."),
    ("웹 > API", "목록 API 는 offset 과 cursor 중 무엇으로 나누나?",
     "데이터가 자주 바뀌면 cursor 를 쓴다. offset 은 중간에 항목이 들어오면 밀린다."),
    ("웹 > 인증", "access 토큰이 만료되면 어떻게 다시 받나?",
     "401 을 받으면 refresh 토큰으로 새 access 토큰을 받고 원래 요청을 다시 보낸다."),
    ("웹 > 인증", "비밀번호는 어떤 해시로 저장하나?", "bcrypt·argon2 처럼 느린 해시에 솔트를 붙여 저장한다."),
    ("웹 > 인증", "세션 쿠키에 SameSite=Lax 를 쓰는 이유는?", "다른 사이트에서 보낸 POST 에는 쿠키가 붙지 않아 CSRF 를 막는다."),
    ("웹 > 인증", "OAuth 에서 PKCE 를 쓰는 이유는?", "인가 코드를 가로채도 code_verifier 없이는 토큰으로 바꿀 수 없게 한다."),
    ("웹 > 배포", "비밀값은 설정 파일과 환경변수 중 어디에 두나?",
     "환경변수나 비밀 저장소에 둔다. 저장소에 올리는 설정 파일에는 넣지 않는다."),
    ("웹 > 배포", "설정만 고쳤는데 옛 주소로 붙는 이유는?",
     "CORS origin, 기본값, 토픽 이름처럼 소스에 박힌 주소는 설정을 바꿔도 그대로 남는다."),
    ("일반 지식 > k8s", "k8s 에서 Pod 와 Deployment 의 차이",
     "Pod 는 컨테이너 실행 단위이고 Deployment 는 Pod 복제본 수와 롤링 업데이트를 관리한다."),
]

DEMO_SYNONYMS = [
    ["토큰", "token"],
    ["인증", "auth", "로그인"],
    ["배포", "deploy"],
    ["디플로이먼트", "deployment"],
]


def build(folder: Path, port: int = 8766, analyzer=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("kb.sqlite3*"):
        old.unlink()
    conn = connect(folder / "kb.sqlite3")
    try:
        user, claude = Store(conn, "user", analyzer), Store(conn, "claude", analyzer)
        for path in dict.fromkeys(p for p, _, _ in DEMO_QA):
            top, mid = [s.strip() for s in path.split(">")]
            if top not in {c["name"] for c in user.categories()}:
                user.add_category(top)
            user.add_category(mid, parent=top)
        for path, q, a in DEMO_QA:
            user.add_qa(path, q, a, force=True)
        for group in DEMO_SYNONYMS:
            user.dict_action("synonym_add", terms=group)
        ev = folder / "근거 폴더" / "cors.ts"
        ev.parent.mkdir(exist_ok=True)
        ev.write_text('export const allowedOrigins = ["https://app.example.com"];\n', encoding="utf-8")
        cors = claude.add_qa("웹 > API", "CORS 허용 출처는 어디서 정하나?", "src/server/cors.ts 의 allowedOrigins 배열에서 정한다.",
                             evidence=[{"kind": "auto", "path": str(ev), "anchor": "allowedOrigins"}], force=True)["item"]
        claude.add_qa("일반 지식 > k8s", "ConfigMap 을 바꿨는데 반영이 안 되는 이유는?",
                      "환경변수로 주입된 값은 Pod 가 재시작되기 전까지 갱신되지 않는다.",
                      evidence=[{"kind": "url", "url": "https://kubernetes.io/docs/concepts/configuration/configmap/"}])
        claude.add_qa("일반 지식 > k8s", "이모지 😀 가 든 질문에서 CORS 강조가 맞는가?",
                      "😀😀 CORS 위치가 밀리지 않아야 한다.", force=True)
        claude.add_qa("웹 > 배포", "가" * 150, "나" * 199 + "\n" + "다" * 200, force=True)
        claude.add_qa("웹 > 인증", "refresh 토큰 수명은 누가 정했나?", "팀 회의에서 30일로 정했다.",
                      evidence=[{"kind": "note", "text": "사용자가 회의록으로 확인"}], force=True)
        user.link(1, 2)
        user.link(1, cors["id"])
        q = claude.get_qa(5)
        claude.update_qa(5, q["version"], answer=q["answer"] + " (Claude 가 고침)", note="표현 정리")
        # 근거 파일이 바뀌어 재확인 대상이 된다
        ev.write_text('export const allowedOrigins = ["https://app.example.com", "http://localhost:5173"];\n',
                      encoding="utf-8")
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
