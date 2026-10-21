"""config.json 을 읽어 실행 설정을 만든다. 상대 경로는 프로젝트 루트 기준이다.

mode 가 local(기본)이면 db_path 하나를 쓴다. remote 면 계정마다 data_dir/users/<id>/kb.sqlite3 를 쓰고
public_url(https://도메인, 경로 없음)의 Host 만 받는다.
원격 모드 로그인: oauth.github.client_id, oauth.google.client_id. client secret 은 config 에 두지 않고
환경 변수 FQ_GITHUB_SECRET, FQ_GOOGLE_SECRET 으로 받는다(web/auth.py). allowed_logins 가 비어 있지 않으면
그 목록("github:<숫자 id>", "google:<sub>")의 신원만 로그인한다.
backup_s3_bucket 이 있으면 원격 모드 주기 작업이 새 백업 사본을 s3://<bucket>/<backup_s3_prefix>users/<id>/ 에 올린다
(kb/s3backup.py). prefix 가 비어 있지 않으면 끝에 / 를 붙인다.
limits 는 원격 모드 요청 제한(분당, web/limits.py DEFAULT_LIMITS 의 키), corpus_cache 는 검색 메모리 사본 상한
(kb/search.py configure 의 max_dbs·max_docs)이다. 둘 다 준 키만 기본값을 덮고, 모르는 키나 양의 정수가 아닌 값은 거부한다.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "db_path": "data/kb.sqlite3",
    "host": "127.0.0.1",
    "port": 8765,
    "backup_dir": "data/backup",
    "backup_keep": 14,
    "log_dir": "data/logs",
    "log_keep_days": 14,
    "recheck_interval_hours": 24,
    "mode": "local",
    "public_url": "",
    "data_dir": None,
    "oauth": {},
    "allowed_logins": [],
    "backup_s3_bucket": "",
    "backup_s3_prefix": "",
    "limits": {},
    "corpus_cache": {},
}
MODES = ("local", "remote")
LIMIT_KEYS = ("mcp", "register", "read", "write", "anon")
CORPUS_CACHE_KEYS = ("max_dbs", "max_docs")


@dataclass(frozen=True)
class Config:
    db_path: Path
    host: str
    port: int
    backup_dir: Path
    backup_keep: int
    log_dir: Path
    log_keep_days: int
    recheck_interval_hours: int
    mode: str = "local"
    public_url: str = ""
    data_dir: Path | None = None
    github_client_id: str = ""
    google_client_id: str = ""
    allowed_logins: frozenset[str] = frozenset()
    backup_s3_bucket: str = ""
    backup_s3_prefix: str = ""
    limits: dict = field(default_factory=dict)
    corpus_cache: dict = field(default_factory=dict)

    @property
    def web_url(self) -> str:
        """웹 화면 주소. 원격 모드는 public_url, 로컬 모드는 http://127.0.0.1:<port>."""
        return self.public_url if self.mode == "remote" else f"http://127.0.0.1:{self.port}"


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.json"
    raw = dict(DEFAULTS)
    if path.exists():
        # 윈도우에서 만든 JSON 은 BOM 이 붙어 온다
        raw.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return Config(
        db_path=_path(raw["db_path"]),
        host=str(raw["host"]),
        port=int(raw["port"]),
        backup_dir=_path(raw["backup_dir"]),
        backup_keep=int(raw["backup_keep"]),
        log_dir=_path(raw["log_dir"]),
        log_keep_days=int(raw["log_keep_days"]),
        recheck_interval_hours=int(raw["recheck_interval_hours"]),
        mode=_mode(raw),
        public_url=_public_url(raw),
        data_dir=None if raw["data_dir"] is None else _path(raw["data_dir"]),
        github_client_id=str((raw["oauth"].get("github") or {}).get("client_id", "")),
        google_client_id=str((raw["oauth"].get("google") or {}).get("client_id", "")),
        allowed_logins=frozenset(str(x) for x in raw["allowed_logins"]),
        backup_s3_bucket=str(raw["backup_s3_bucket"]).strip(),
        backup_s3_prefix=_prefix(raw["backup_s3_prefix"]),
        limits=_counts(raw, "limits", LIMIT_KEYS),
        corpus_cache=_counts(raw, "corpus_cache", CORPUS_CACHE_KEYS),
    )


def _counts(raw: dict, key: str, allowed: tuple[str, ...]) -> dict[str, int]:
    value = raw[key]
    if not isinstance(value, dict):
        raise ValueError(f"config 의 {key} 는 객체다: {value!r}")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"config 의 {key} 에 모르는 키가 있다: {', '.join(unknown)} (쓸 수 있는 키: {', '.join(allowed)})")
    bad = [k for k, v in value.items() if not isinstance(v, int) or isinstance(v, bool) or v <= 0]
    if bad:
        raise ValueError(f"config 의 {key}.{bad[0]} 는 양의 정수다: {value[bad[0]]!r}")
    return dict(value)


def _prefix(value) -> str:
    value = str(value or "").strip().lstrip("/")
    return value if not value or value.endswith("/") else value + "/"


def _mode(raw: dict) -> str:
    mode = raw["mode"]
    if mode not in MODES:
        raise ValueError(f"config 의 mode 는 {'·'.join(MODES)} 중 하나다: {mode!r}")
    if mode == "remote" and not raw["data_dir"]:
        raise ValueError("mode 가 remote 면 data_dir(사용자 DB 폴더)이 필요하다.")
    return mode


def _public_url(raw: dict) -> str:
    url = str(raw["public_url"]).rstrip("/")
    if raw["mode"] != "remote":
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.path or parts.query:
        raise ValueError(f"mode 가 remote 면 public_url 은 https://도메인 형식이다(경로 없음): {url!r}")
    return url
