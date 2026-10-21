# fast-question 원격 서버 배포

원격 모드(`mode: remote`) 서버를 인스턴스 1대에서 Docker 로 띄우는 파일이다. AWS 자원(인스턴스, S3 버킷, DNS)은 이 폴더가 만들지 않는다.

| 파일 | 내용 |
|---|---|
| `Dockerfile` | `python:3.12-slim`, `requirements.txt` + `requirements-server.txt`(boto3, anthropic), 비루트 사용자 `fq`, 데이터 볼륨 `/data`, `python run_web.py --config /etc/fq/config.json` |
| `compose.yaml` | `app`(8765, 컨테이너 안에서만 열림) + `caddy`(80·443, Let's Encrypt 인증서) |
| `Caddyfile` | `FQ_DOMAIN` → `app:8765`, `www.FQ_DOMAIN` → 본 주소로 넘김 |
| `config.example.json` | 원격 모드 설정 예시. `config.json` 으로 복사해 고친다 |

빌드 문맥은 저장소 루트이고 루트의 `.dockerignore` 가 `data`, `docs`, `tests`, `.venv` 등을 뺀다.

서버 쪽 명령(설정·검사와 기동·형태소 사용자 단어)은 서버(Linux 인스턴스)의 저장소 루트에서 실행한다. bash 와 PowerShell 에서 똑같이 돈다.

## 설정

1. `config.example.json` 을 같은 폴더의 `config.json` 으로 복사한다. `config.json` 은 `.gitignore` 대상이다.

   ```powershell
   cp deploy/config.example.json deploy/config.json
   ```

2. `config.json` 에서 고칠 키
   - `public_url`: `https://` + 실제 도메인. `deploy/.env` 의 `FQ_DOMAIN` 과 같아야 한다(다른 Host 의 요청은 403).
   - `oauth.github.client_id`, `oauth.google.client_id`: 로그인 공급자의 client id.
   - `allowed_logins`: 비어 있지 않으면 그 신원(`github:<숫자 id>`, `google:<sub>`)만 로그인한다.
   - `backup_s3_bucket`, `backup_s3_prefix`: 비어 있지 않으면 일별 백업 사본을 `s3://<bucket>/<prefix>users/<id>/kb-YYYYMMDD.sqlite3`, `<prefix>control/control-YYYYMMDD.sqlite3` 에 올린다.
   - `limits`: 분당 요청 제한. `mcp`(`/mcp`, 계정 또는 토큰당), `register`(OAuth 동적 등록, IP 당), `read`·`write`(`/api/*` 읽기·쓰기, 계정당), `anon`(로그인하지 않은 `/api/*`, IP 당). 예시 값이 기본값이고, 뺀 키는 기본값을 쓴다.
   - `corpus_cache`: 검색 메모리 사본 상한. `max_dbs`(사본을 둘 사용자 DB 수), `max_docs`(사본 항목 합계). 넘으면 가장 오래 쓰지 않은 DB 부터 비운다. 예시 값이 기본값이다.
   - 두 키 모두 모르는 키나 양의 정수가 아닌 값이면 서버가 뜨지 않는다.
   - `host`(`0.0.0.0`), `data_dir`(`/data`), `log_dir`(`/data/logs`)는 이미지 구조에 맞춘 값이라 그대로 둔다.
3. `deploy/.env` 에 `FQ_DOMAIN=<도메인>`(https:// 없이)을 적는다. Caddyfile 은 고치지 않는다(서버의 git pull 이 충돌하지 않게).
4. 비밀값은 config 에 넣지 않는다. `compose.yaml` 이 다음 환경 변수를 컨테이너로 넘긴다. `deploy/.env` 파일(`이름=값` 한 줄씩)이나 셸 환경 변수로 준다.
   - `FQ_GITHUB_SECRET`, `FQ_GOOGLE_SECRET`: 로그인 공급자의 client secret
   - `AWS_DEFAULT_REGION`: S3 리전(기본 `ap-northeast-2`)
   - 인스턴스 역할이 없는 환경은 `deploy/aws.env` 에 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` 를 적는다. 파일이 없으면 건너뛴다(`env_file` 의 `required: false`, Docker Compose 2.24 이상).

## 검사와 기동

설정 문법 검사(이미지를 만들지 않는다)

```powershell
docker compose -f deploy/compose.yaml config
docker build --check -f deploy/Dockerfile .
```

기동(저장소 루트에서). 첫 실행은 이미지를 만든다.

```powershell
docker compose -f deploy/compose.yaml up -d --build
```

- 상태 점검: `https://<도메인>/healthz` 가 200 이면 정상, 503 이면 응답 JSON 의 `db`·`disk_free`·`morph` 중 실패한 항목을 본다. 컨테이너의 `HEALTHCHECK` 도 같은 경로를 1분마다 부른다.
- 로그: 앱 로그는 표준 에러로 나가 `docker compose -f deploy/compose.yaml logs app` 로 본다. `/data/logs/web.log` 에도 남는다.
- 데이터: 볼륨 `fq-data` 의 `/data/control.sqlite3`(계정·세션·토큰), `/data/users/<id>/kb.sqlite3`(사용자 KB), `/data/users/<id>/backup/`, `/data/backup/`(제어 DB 사본).
- 웹 Claude 패널: 사용자가 등록한 Anthropic API 키는 제어 DB 에 암호화해 두고, 암호 키는 처음 등록할 때 `/data/claude-key.secret` 으로 생긴다. 이 파일은 S3 로 올리지 않는다. 잃거나 바꾸면 저장된 키를 풀 수 없어 사용자가 키를 다시 등록해야 한다.
- 주기 작업: 10분마다 기한이 된 사용자별 백업(하루 1번)·재색인과 제어 DB 백업을 돌린다. 근거 재검증은 사용자 PC 에서 한다.
  배포로 기본 가이드(`kb/default_guide.json`)가 바뀌면 사용자 DB 마다 한 번 다시 넣는다. 사용자나 Claude 가 고친 가이드 항목은 그대로 둔다.
  하루 한 번 30일 넘게 판정되지 않은 근거를 unknown 으로 바꾸고, 1년 안 쓴 OAuth 클라이언트, 등록하고 하루 안에 토큰을 받지 않은 OAuth 클라이언트, 30일 지난 탈퇴 폴더(`/data/deleted/`)를 지운다.
  하루 한 번 사용자 DB 마다 정리 대상(질문이 비슷한 항목, 지금 형식 규칙에 어긋나는 Claude 등록분)을 찾아 `cleanup_finding` 표에 둔다. 항목은 고치지 않는다. 처리는 Claude 가 MCP `kb_cleanup_list`·`kb_cleanup_resolve` 로 한다.
  사용자 DB 의 첫 탐지는 전부를 비교해 2만 건에 약 20초 CPU 를 쓰고, 그 뒤로는 바뀐 항목만 본다.
- 계정당 상한: Q&A 20,000건, 분류 500개, 근거 100,000행(삭제한 것 포함), DB 파일 200 MB(보관·복원·연결 끊기 외의 쓰기를 막는다), 개인 토큰 10개. OAuth 연결은 20개이고, 넘으면 가장 오래 안 쓴 연결을 끊는다.
- 앱은 Caddy 뒤에서만 받는다(compose 의 app 은 `expose` 만 한다). 요청·가입 제한이 실제 IP 로 걸리도록 원격 모드의 uvicorn 은 `X-Forwarded-For` 를 믿는다.

## 자동 배포 (GitHub Actions)

main 에 push 하면 `.github/workflows/deploy.yml` 이 테스트를 돌리고, 통과하면 SSH 로 서버의 `deploy/deploy.sh` 를 실행한다. Actions 화면의 deploy → Run workflow 로 손으로도 돌린다.

`deploy/deploy.sh` 가 하는 일
- 저장소를 `origin/main` 으로 맞춘다(`git fetch` + `reset --hard`). `git clean` 은 하지 않아 `deploy/config.json`·`.env`·`aws.env` 는 그대로다.
- `docker compose -f deploy/compose.yaml up -d --build` 로 다시 띄운다.
- 컨테이너 안에서 `/healthz` 가 200 이 될 때까지 3분 기다린다. 넘으면 앱 로그 50줄을 내고 실패한다.
- 손으로 배포할 때도 저장소 루트에서 `sh deploy/deploy.sh` 를 실행한다.

처음 한 번 하는 설정(서버 bash, 저장소는 `~/fast-question`)

```bash
cd ~/fast-question && git fetch origin && git checkout -B main origin/main
ssh-keygen -t ed25519 -N "" -C fq-github-deploy -f ~/.ssh/fq_deploy
printf 'command="sh %s/fast-question/deploy/deploy.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding %s\n' "$HOME" "$(cat ~/.ssh/fq_deploy.pub)" >> ~/.ssh/authorized_keys
echo "DEPLOY_HOST: $(whoami)@<도메인>"
echo "DEPLOY_KNOWN_HOSTS: <도메인> $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
cat ~/.ssh/fq_deploy
```

- 이 키는 강제 명령으로 등록되어, 무엇을 보내든 `deploy/deploy.sh` 만 실행한다(셸·포트 전달 없음).
- GitHub 저장소 Settings → Environments 에 `production` 을 만들고 secret 세 개를 넣는다.
  - `DEPLOY_HOST`: 위에 출력된 `사용자@도메인`
  - `DEPLOY_KNOWN_HOSTS`: 위에 출력된 한 줄(`<도메인>` 을 실제 도메인으로)
  - `DEPLOY_SSH_KEY`: `cat ~/.ssh/fq_deploy` 출력 전체
- secret 을 넣은 뒤 서버의 개인키 `~/.ssh/fq_deploy` 는 지운다(`rm ~/.ssh/fq_deploy`). 공개키와 authorized_keys 줄은 남긴다.
- 키를 바꾸거나 끊으려면 authorized_keys 에서 `fq-github-deploy` 줄을 지운다.

## 형태소 사용자 단어

원격 모드의 Kiwi 사용자 단어는 서버 전역 사전 하나다(제어 DB 의 `global_user_word` 표). 사용자가 MCP `kb_dict` 로 바꾸는 것은 거부된다. 운영자가 컨테이너 안에서 바꾼다.

```powershell
docker compose -f deploy/compose.yaml exec app python scripts/global_words.py --config /etc/fq/config.json list
docker compose -f deploy/compose.yaml exec app python scripts/global_words.py --config /etc/fq/config.json add 망연계 NNP
docker compose -f deploy/compose.yaml exec app python scripts/global_words.py --config /etc/fq/config.json remove 망연계
```

바꾸면 다음 검색·쓰기 때 Kiwi 를 한 번 다시 띄우고(약 2초), 주기 작업이 모든 사용자 DB 를 재색인한다.

## 로컬 DB 옮기기

사용자 PC(Windows)의 저장소 루트에서 PowerShell 로 실행한다. 서버 주소와 개인 토큰은 `%USERPROFILE%\.fast-question\credentials.json` 에서 읽는다.

```powershell
.venv\Scripts\python.exe scripts\push_local.py --dry-run
.venv\Scripts\python.exe scripts\push_local.py
.venv\Scripts\python.exe scripts\push_local.py --exclude-category "회사 > 내부"
```

- `data\kb.sqlite3`(또는 `--db <파일>`)의 온라인 백업 사본을 만들어 `POST /api/import/db` 로 올린다. 로컬 DB 는 바꾸지 않는다.
- 사본에서 로컬 기본 가이드(system 이 만든 항목)는 늘 지우고 올린다. `--exclude-category <분류>`(여러 번 가능, `대분류` 는 하위 분류 포함)로 준 분류도 지운다. 보관한 항목·이력·검색 색인까지 지우므로 그 내용은 서버로 가지 않는다.
- `--dry-run` 은 `POST /api/import/db/plan` 으로 보내 서버가 할 일(통째로 바꾸기 / 하나씩 넣기)과 건수, 건너뛸 목록만 받는다. 서버에 쓰지 않는다.
- 서버의 내 DB 가 비어 있으면(기본 가이드만 있으면) 파일 내용을 통째로 바꾸고 원격 기본 가이드를 다시 넣는다. 아니면 항목을 하나씩 넣고 이미 있는 항목과 비슷한 질문은 건너뛴 목록을 보여 준다.
- 손상됐거나 스키마 버전·표 목록이 서버와 다른 파일은 거부한다. 상한은 200 MB 다.
- 통째로 바꿀 때는 올린 파일의 행 수와 크기가 계정당 상한 안이어야 한다(미리보기도 같이 거부한다). 하나씩 넣을 때는 상한에 닿은 항목부터 넣지 못한 목록에 든다.
- 한 계정의 가져오기(미리보기 포함)는 한 번에 하나다. 진행 중에 또 보내면 409 로 거부한다.
