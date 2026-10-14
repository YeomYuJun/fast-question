"""웹 서버 진입점. 쓰기 주체는 user 다.

로그는 config 의 log_dir(기본 data/logs)에 web.log 로 남기고 자정마다 넘겨 log_keep_days 일치를 둔다.
pythonw 로 띄우면(scripts/autostart.py) 표준 출력이 없어 로그 파일에만 쓴다.
사용: <venv python> run_web.py [--config <config.json>]
"""
import argparse
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from kb.config import Config, load_config  # noqa: E402
from web.app import create_app  # noqa: E402


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [TimedRotatingFileHandler(
        cfg.log_dir / "web.log", when="midnight", backupCount=cfg.log_keep_days, encoding="utf-8")]
    if sys.stderr is None:  # pythonw: 표준 출력이 없으면 print·경고가 실패하지 않게 버린다
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=handlers, force=True)


def seed_guide(cfg: Config, log: logging.Logger) -> None:
    """기본 사용 가이드를 넣거나 갱신한다. git pull 뒤 재시작하면 새 가이드가 반영된다. 실패해도 서버는 뜬다.
    원격 모드는 여기서 넣지 않는다. 사용자 DB 를 처음 만들 때 web/app.py 가 원격용 가이드(근거 없음)를 넣는다."""
    if cfg.mode == "remote":
        return
    from kb import guide
    from kb.db import connect
    from kb.store import Store
    try:
        conn = connect(cfg.db_path)
        try:
            log.info("기본 가이드: %s", guide.seed(Store(conn, "system")))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("기본 가이드를 넣지 못했다")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    cfg = load_config(ap.parse_args().config)
    setup_logging(cfg)
    log = logging.getLogger("fast-question.web")
    log.info("시작: http://%s:%s, %s", cfg.host, cfg.port,
             f"원격 모드 {cfg.public_url}, 데이터 {cfg.data_dir}" if cfg.mode == "remote" else f"DB {cfg.db_path}")
    seed_guide(cfg, log)
    # log_config=None: uvicorn 로그도 위 핸들러로 간다. 접근 로그는 warning 수준이라 남지 않는다
    # 원격 모드는 역방향 프록시(Caddy) 뒤에서만 돈다(deploy/compose.yaml 의 app 은 expose 만 하고 밖으로 열지 않는다).
    # 프록시가 넣은 X-Forwarded-For 로 실제 클라이언트 IP 를 써야 요청 제한·가입 제한이 IP 별로 걸린다
    proxied = {"proxy_headers": True, "forwarded_allow_ips": "*"} if cfg.mode == "remote" else {}
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", log_config=None, **proxied)


if __name__ == "__main__":
    main()
