"""원격 모드 백업 사본을 S3 에 올린다. 키는 <prefix>users/<계정 id>/kb-YYYYMMDD.sqlite3 와
<prefix>control/control-YYYYMMDD.sqlite3 다. 버킷의 버전 관리·수명 주기는 AWS 쪽 설정이다.

boto3 는 서버 이미지에만 설치한다(requirements-server.txt). 로컬 설치에는 없으므로 client() 안에서 불러온다.
자격 증명은 boto3 의 기본 탐색(인스턴스 역할, AWS_* 환경 변수)을 쓴다.
"""
from pathlib import Path


def client():
    import boto3
    return boto3.client("s3")


def user_key(prefix: str, account_id: int, backup_file: Path) -> str:
    # 윈도우에서도 키 구분자가 / 이도록 Path 로 잇지 않는다
    return f"{prefix}users/{account_id}/{backup_file.name}"


def control_key(prefix: str, backup_file: Path) -> str:
    return f"{prefix}control/{backup_file.name}"


def upload(s3, bucket: str, key: str, path: Path) -> dict:
    s3.upload_file(str(path), bucket, key)
    return {"url": f"s3://{bucket}/{key}"}
