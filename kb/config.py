"""config.json 을 읽어 실행 설정을 만든다. 상대 경로는 프로젝트 루트 기준이다."""
import json
from dataclasses import dataclass
from pathlib import Path

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
}


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
    )
