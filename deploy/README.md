# fast-question 원격 서버 배포

원격 모드(`mode: remote`) 서버를 인스턴스 1대에서 Docker 로 띄우는 파일이다. AWS 자원(인스턴스, S3 버킷, DNS)은 이 폴더가 만들지 않는다.

| 파일 | 내용 |
|---|---|
| `Dockerfile` | `python:3.12-slim`, `requirements.txt` + `requirements-server.txt`(boto3), 비루트 사용자 `fq`, 데이터 볼륨 `/data`, `python run_web.py --config /etc/fq/config.json` |
| `compose.yaml` | `app`(8765, 컨테이너 안에서만 열림) + `caddy`(80·443, Let's Encrypt 인증서) |
| `Caddyfile` | 도메인 자리표시자 `www.domain-example.com` → `app:8765` |
| `config.example.json` | 원격 모드 설정 예시. `config.json` 으로 복사해 고친다 |

빌드 문맥은 저장소 루트이고 루트의 `.dockerignore` 가 `data`, `docs`, `tests`, `.venv` 등을 뺀다.

## 설정

1. `config.example.json` 을 같은 폴더의 `config.json` 으로 복사한다. `config.json` 은 `.gitignore` 대상이다.

   ```powershell
   Copy-Item "deploy\config.example.json" "deploy\config.json"
   ```

2. `config.json` 에서 고칠 키
   - `public_url`: `https://` + 실제 도메인. `Caddyfile` 의 도메인과 같아야 한다(다른 Host 의 요청은 403).
   - `oauth.github.client_id`, `oauth.google.client_id`: 로그인 공급자의 client id.
   - `allowed_logins`: 비어 있지 않으면 그 신원(`github:<숫자 id>`, `google:<sub>`)만 로그인한다.
   - `backup_s3_bucket`, `backup_s3_prefix`: 비어 있지 않으면 일별 백업 사본을 `s3://<bucket>/<prefix>users/<id>/kb-YYYYMMDD.sqlite3`, `<prefix>control/control-YYYYMMDD.sqlite3` 에 올린다.
   - `host`(`0.0.0.0`), `data_dir`(`/data`), `log_dir`(`/data/logs`)는 이미지 구조에 맞춘 값이라 그대로 둔다.
3. `Caddyfile` 의 `www.domain-example.com` 을 실제 도메인으로 바꾼다.
4. 비밀값은 config 에 넣지 않는다. `compose.yaml` 이 다음 환경 변수를 컨테이너로 넘긴다. `deploy\.env` 파일(`이름=값` 한 줄씩)이나 셸 환경 변수로 준다.
   - `FQ_GITHUB_SECRET`, `FQ_GOOGLE_SECRET`: 로그인 공급자의 client secret
   - `AWS_DEFAULT_REGION`: S3 리전(기본 `ap-northeast-2`)
   - 인스턴스 역할이 없는 환경은 `deploy\aws.env` 에 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` 를 적는다. 파일이 없으면 건너뛴다(`env_file` 의 `required: false`, Docker Compose 2.24 이상).

## 검사와 기동

설정 문법 검사(이미지를 만들지 않는다)

```powershell
docker compose -f "deploy\compose.yaml" config
docker build --check -f "deploy\Dockerfile" .
```

기동(저장소 루트에서). 첫 실행은 이미지를 만든다.

```powershell
docker compose -f "deploy\compose.yaml" up -d --build
```

- 상태 점검: `https://<도메인>/healthz` 가 200 이면 정상, 503 이면 응답 JSON 의 `db`·`disk_free`·`morph` 중 실패한 항목을 본다. 컨테이너의 `HEALTHCHECK` 도 같은 경로를 1분마다 부른다.
- 로그: 앱 로그는 표준 에러로 나가 `docker compose -f "deploy\compose.yaml" logs app` 로 본다. `/data/logs/web.log` 에도 남는다.
- 데이터: 볼륨 `fq-data` 의 `/data/control.sqlite3`(계정·세션·토큰), `/data/users/<id>/kb.sqlite3`(사용자 KB), `/data/users/<id>/backup/`, `/data/backup/`(제어 DB 사본).
- 주기 작업: 10분마다 기한이 된 사용자별 백업(하루 1번)·재색인과 제어 DB 백업을 돌린다. 근거 재검증은 사용자 PC 에서 한다.

## 형태소 사용자 단어

원격 모드의 Kiwi 사용자 단어는 서버 전역 사전 하나다(제어 DB 의 `global_user_word` 표). 사용자가 MCP `kb_dict` 로 바꾸는 것은 거부된다. 운영자가 컨테이너 안에서 바꾼다.

```powershell
docker compose -f "deploy\compose.yaml" exec app python scripts/global_words.py --config /etc/fq/config.json list
docker compose -f "deploy\compose.yaml" exec app python scripts/global_words.py --config /etc/fq/config.json add 망연계 NNP
docker compose -f "deploy\compose.yaml" exec app python scripts/global_words.py --config /etc/fq/config.json remove 망연계
```

바꾸면 다음 검색·쓰기 때 Kiwi 를 한 번 다시 띄우고(약 2초), 주기 작업이 모든 사용자 DB 를 재색인한다.

## 로컬 DB 옮기기

사용자 PC 에서 저장소 루트에서 실행한다. 서버 주소와 개인 토큰은 `%USERPROFILE%\.fast-question\credentials.json` 에서 읽는다.

```powershell
.venv\Scripts\python.exe scripts\push_local.py
```

- `data\kb.sqlite3`(또는 `--db <파일>`)의 온라인 백업 사본을 만들어 `POST /api/import/db` 로 올린다. 로컬 DB 는 바꾸지 않는다.
- 서버의 내 DB 가 비어 있으면(기본 가이드만 있으면) 파일 내용을 통째로 바꾼다. 아니면 항목을 하나씩 넣고 이미 있는 항목과 비슷한 질문은 건너뛴 목록을 보여 준다.
- 손상됐거나 스키마 버전·표 목록이 서버와 다른 파일은 거부한다. 상한은 200 MB 다.
