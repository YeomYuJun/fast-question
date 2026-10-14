"""deploy/ 의 배포 파일이 지금 코드와 맞는지 본다. 이미지 빌드는 하지 않는다."""
import re
from pathlib import Path

from kb.config import load_config

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


def test_example_config_is_valid_remote_config():
    cfg = load_config(DEPLOY / "config.example.json")
    assert cfg.mode == "remote" and cfg.host == "0.0.0.0" and cfg.public_url.startswith("https://")
    assert cfg.data_dir is not None and cfg.log_dir.as_posix().endswith("/data/logs")  # 비루트 사용자가 쓸 수 있는 곳
    assert cfg.backup_s3_bucket == ""


def test_dockerfile_copies_existing_paths_not_ignored():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    copied = [src for line in re.findall(r"^COPY (.+)$", text, re.M) for src in line.split()[:-1]]
    assert {"kb", "web", "mcp_server", "run_web.py", "requirements.txt", "requirements-server.txt"} <= set(copied)
    for src in copied:
        assert (ROOT / src).exists(), src
        assert src not in ignored, src
    assert "!scripts/global_words.py" in ignored  # 운영자 명령은 컨테이너 안에서 돈다
    assert "USER fq" in text and 'VOLUME ["/data"]' in text
    assert '"--config", "/etc/fq/config.json"' in text


def test_boto3_only_in_server_requirements():
    server = (ROOT / "requirements-server.txt").read_text(encoding="utf-8")
    assert re.search(r"^boto3==\d+\.\d+\.\d+$", server, re.M)
    assert "boto3" not in (ROOT / "requirements.txt").read_text(encoding="utf-8")
