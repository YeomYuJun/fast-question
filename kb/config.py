"""config.json 을 읽어 실행 설정을 만든다. 상대 경로는 프로젝트 루트 기준이다.

mode 가 local(기본)이면 db_path 하나를 쓴다. remote 면 계정마다 data_dir/users/<id>/kb.sqlite3 를 쓰고
public_url(https://도메인, 경로 없음)의 Host 만 받는다.
"""
import json
from dataclasses import dataclass
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
}
MODES = ("local", "remote")


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
    )


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
