"""원격 모드의 사용자별 파일 위치. 계정 하나가 KB 파일 하나(data_dir/users/<id>/kb.sqlite3)를 쓴다.

계정 id 는 제어 DB(kb/control.py) account 표의 양의 정수만 받는다. 문자열을 받지 않아 경로 조각이 끼어들 수 없다.
탈퇴한 계정 폴더는 deleted/<id>-<UTC 시각>/ 으로 옮긴다.
"""
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

KB_FILE = "kb.sqlite3"


def _account_id(account_id) -> int:
    if type(account_id) is not int or account_id <= 0:  # bool 은 int 의 하위형이라 type 으로 막는다
        raise ValueError(f"계정 id 는 양의 정수다: {account_id!r}")
    return account_id


def control_path(data_dir: Path) -> Path:
    return data_dir / "control.sqlite3"


def user_dir(data_dir: Path, account_id: int) -> Path:
    return data_dir / "users" / str(_account_id(account_id))


def db_path_for(data_dir: Path, account_id: int) -> Path:
    return user_dir(data_dir, account_id) / KB_FILE


def list_user_dbs(data_dir: Path) -> list[tuple[int, Path]]:
    """KB 파일이 있는 계정만 (id, 경로) 로, id 순으로."""
    users = data_dir / "users"
    if not users.is_dir():
        return []
    found = [(int(d.name), d / KB_FILE) for d in users.iterdir()
             if d.name.isdigit() and (d / KB_FILE).is_file()]
    return sorted(found)


def retire(data_dir: Path, account_id: int, now: datetime | None = None) -> Path:
    """계정 폴더를 deleted/ 로 옮기고 옮긴 위치를 돌려준다."""
    src = user_dir(data_dir, account_id)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    dst = data_dir / "deleted" / f"{account_id}-{stamp}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return dst


def purge_deleted(data_dir: Path, days: int = 30, now: datetime | None = None) -> list[str]:
    """deleted/ 아래에서 옮긴 지 days 일이 지난 탈퇴 폴더를 지우고 지운 폴더 이름을 돌려준다."""
    root = data_dir / "deleted"
    if not root.is_dir():
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    gone = []
    for d in sorted(root.iterdir()):
        _, _, stamp = d.name.partition("-")
        try:
            moved = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
        except ValueError:  # 이 함수가 만든 이름이 아니면 건드리지 않는다
            continue
        if moved < cutoff and d.is_dir():
            shutil.rmtree(d)
            gone.append(d.name)
    return gone
