import pytest

from kb.db import connect

# 테스트 표본 데이터 11건. id 는 넣는 순서대로 1~11 이 된다.
SAMPLE_QA = [
    ("이관 > NAS", ".nas-tmp 가 NAS 안에 있는 이유", "rename 은 같은 볼륨 안에서만 원자적이다. /tmp 같은 로컬 디스크에 쓰고 옮기면 복사 후 삭제가 되어 원자성이 깨진다."),
    ("이관 > NAS", ".part 에 먼저 쓰는 이유", "파일시스템은 쓰는 도중의 파일도 다른 Pod 에서 읽힌다. 다 쓴 뒤 ATOMIC_MOVE 로 rename 해서 확정한다."),
    ("이관 > NAS", "쓰기 전에 여유 공간을 검사하는 이유(507)", "NAS 는 볼륨 용량이 정해져 있다. 쓰기 전에 getUsableSpace() 로 남은 용량을 확인한다."),
    ("이관 > NAS", "접두어 검사, 확장자 차단, 413 은 NAS 특성인가", "아니다. NAS 와 무관한 애플리케이션 정책이다."),
    ("이관 > DB 암호화", "PETRA 를 왜 제거하나", "이관 후 대상 DB 에서 PETRA 를 쓸 수 없어 애플리케이션이 ARIA 로 직접 암·복호화한다."),
    ("이관 > DB 암호화", "ARIA 암호화 방식은", "ARIA-256/CBC/PKCS#5, 고정 IV, 결과는 \"A1\"+Base64 형식이다."),
    ("이관 > DB 암호화", "PLS_ 함수 호출은 무엇으로 바뀌나", "매퍼 XML 의 PLS_* 호출 자리를 MyBatis TypeHandler 가 대신한다."),
    ("이관 > DB 암호화", "데이터 이관 순서는", "PETRA 로 복호화한 뒤 평문으로 옮기고 대상에서 ARIA 로 재암호화한다."),
    ("이관 > 네트워크", "API Gateway 라우팅 룰은 어디서 가져오나", "NHN 콘솔에만 있어 코드에서 도출할 수 없다. 현행 룰 export 를 받아 인프라팀에 넘긴다."),
    ("이관 > 네트워크", "설정만 고치면 안 되는 이유", "CORS origin, @Value 폴백, Kafka 토픽명처럼 소스에 박힌 주소는 설정을 고쳐도 구 주소로 붙는다."),
    ("일반 지식 > k8s", "k8s 에서 Pod 와 Deployment 의 차이", "Pod 는 컨테이너 실행 단위이고 Deployment 는 Pod 복제본 수와 롤링 업데이트를 관리한다."),
]

SYNONYM_GROUPS = [
    ["임시파일", ".part", "tmp"],
    ["규칙", "룰", "rule"],
    ["디플로이먼트", "deployment"],
    ["스토리지", "storage", "nas"],
]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "kb.sqlite3")
    yield c
    c.close()


def load_sample(store, synonyms=True):
    """표본 데이터(SAMPLE_QA)를 넣는다. 질문끼리 비슷해도 넣도록 force 를 쓴다."""
    for path in dict.fromkeys(p for p, _, _ in SAMPLE_QA):
        top, mid = [s.strip() for s in path.split(">")]
        tops = {c["name"]: c for c in store.categories()}
        if top not in tops:
            store.add_category(top)
        store.add_category(mid, parent=top)
    ids = [store.add_qa(p, q, a, force=True)["item"]["id"] for p, q, a in SAMPLE_QA]
    assert ids == list(range(1, 12))
    if synonyms:
        for group in SYNONYM_GROUPS:
            store.dict_action("synonym_add", terms=group)
    return ids


@pytest.fixture(autouse=True)
def no_real_credentials(tmp_path_factory, monkeypatch):
    """이 PC 에 원격 자격 증명이 있어도 테스트는 읽지 않는다(즉석 질문 훅·프록시가 원격으로 가지 않게)."""
    monkeypatch.setenv("FQ_CREDENTIALS", str(tmp_path_factory.mktemp("cred") / "credentials.json"))
