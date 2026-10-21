# fast-question

작업 중 생긴 질문 1줄과 짧은 답을 쌓고 검색하는 Q&A 지식 DB 다. 사람은 브라우저로, Claude 는 MCP 로 같은 DB 를 쓴다.
원격 서비스 https://fast-question.com 을 쓰거나, 저장소를 받아 PC 에서 직접 띄운다(로컬 모드).

## 원격 MCP 연결

설치 없이 Claude Code 에 원격 MCP 를 연결한다.

```powershell
claude mcp add --transport http -s user fast-question https://fast-question.com/mcp
```

Claude Code 에서 `/mcp` 를 열어 `fast-question` 을 인증한다. 브라우저에서 로그인하고 승인하면 `kb_*` 도구를 쓸 수 있다.

## Claude Code 플러그인

플러그인은 원격 MCP 에 더해 PC 쪽 기능을 설치한다. PC 에 uv(`uvx`)가 있어야 한다.

```powershell
winget install --id astral-sh.uv -e
claude plugin marketplace add YeomYuJun/fast-question
claude plugin install fast-question@fast-question
```

들어 있는 것

- MCP 서버 `fast-question`: 원격 서버의 `kb_*` 도구를 로컬 프록시로 쓴다. 파일·git 근거는 이 PC 에서 캡처하고 판정한다.
- Skill `/fast-question:qa`(등록·검색), `/fast-question:qa-harvest`(프로젝트 폴더에서 Q&A 구축)
- 즉석 질문 훅(UserPromptSubmit): 질문하면 KB 에서 맞는 Q&A 를 찾아 그 턴에 넣는다.

MCP·훅·Skill 명령은 PC 클라이언트 패키지 `fast-question` 을 `uvx` 로 실행한다. 설치 뒤 아래 PC 로그인을 한 번 하고 Claude Code 를 다시 시작한다. 자세한 것은 [plugin/fast-question/README.md](plugin/fast-question/README.md).

## claude.ai·Claude Desktop

claude.ai 의 커스텀 커넥터로 `https://fast-question.com/mcp` 를 추가한다. 등록·검색 절차를 담은 qa Skill 은 저장소에서 파일로 만들어 Claude 에 올린다.

```powershell
.venv\Scripts\python.exe scripts\build_desktop_skill.py
```

- 결과는 `dist/qa.skill` 이다(zip, 안에 `qa/SKILL.md`·`qa/rules.md`).
- `skill/qa` 원본에서 `<!-- remote -->` 구간을 남기고 `<!-- local -->` 구간을 뺀다. 원격 서버는 사용자 PC 의 파일을 읽지 못하므로 파일 근거·근거 재판정·즉석 질문 훅 절차가 빠진다.
- qa-harvest 는 넣지 않는다. 사용자 PC 의 폴더와 PC 클라이언트 명령이 있어야 한다.

## PC 로그인

플러그인의 MCP 와 훅은 개인 토큰으로 서버에 접속한다. 토큰은 브라우저 승인으로 받는다.

```powershell
uvx fast-question login
```

- 브라우저가 서버의 승인 화면을 연다. 열리지 않으면 터미널에 찍힌 주소를 연다.
- 로그인해 승인하면 토큰이 `%USERPROFILE%\.fast-question\credentials.json` 에 저장된다. 5분 안에 승인하지 않으면 끝난다.
- 서버 주소는 `--url` 로 준다. 없으면 credentials.json 의 주소, 그것도 없으면 `https://fast-question.com` 이다.
- 같은 서버에 쓰던 토큰이 있으면 새 토큰을 받으면서 옛 토큰을 서버에서 폐기한다.

## 웹 화면의 Claude

원격 서비스 화면 오른쪽 위 Claude 아이콘을 누르면 오른쪽에 대화 패널이 열린다. Anthropic API 키(Claude Console 에서 만든 키)를 한 번 등록하면 그 키로 Claude 가 이 DB 를 찾고, 등록·수정·보관·분류 정리를 한다.

- 키는 서버에 암호화해 보관하고, 요금은 그 키의 계정에 청구된다.
- 파일 근거 달기, 항목·분류 삭제, 대량 등록은 이 패널에서 하지 않는다.

## 로컬 모드

저장소를 받아 PC 에서 웹 화면과 MCP 를 직접 띄운다. DB 는 SQLite 파일 하나이고 위치는 `config.json` 의 `db_path` 다. Python 3.12 가 필요하다.

Windows(PowerShell)

```powershell
git clone https://github.com/YeomYuJun/fast-question
cd fast-question
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts\install.py
.venv\Scripts\python.exe run_web.py
```

Linux

```sh
git clone https://github.com/YeomYuJun/fast-question
cd fast-question
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/install.py
.venv/bin/python run_web.py
```

- `scripts/install.py` 가 하는 일
  - `skill/` 의 qa·qa-harvest Skill 을 `~/.claude/skills` 로 복사한다.
  - `run_mcp.py` 를 MCP 서버 `fast-question` 으로 Claude Code 사용자 범위에 등록한다(`claude` CLI 가 PATH 에 있어야 한다).
  - 즉석 질문 훅(`hooks/ask_kb.py`)을 `~/.claude/settings.json` 의 UserPromptSubmit 에 등록한다. 처음 고치기 전 원본은 `settings.json.fast-question.bak` 으로 남긴다.
  - DB 에 기본 사용 가이드를 넣는다.
  - MCP·훅·Skill 명령은 저장소의 `.venv` python 으로 실행된다. 저장소를 옮기면 다시 실행한다.
- `run_web.py` 는 http://127.0.0.1:8765 에 웹 화면을 띄운다. 설정은 저장소의 `config.json`(DB 경로, 포트, 백업·로그 폴더)이고, `--config "<다른 config.json>"` 으로 바꾼다.
- `run_mcp.py` 는 Claude Code 가 띄우는 stdio MCP 진입점이다. 직접 실행하지 않는다.
- 훅만 지우기: `scripts/install.py --remove-hook`(위와 같은 `.venv` python 으로 실행)
- 되돌리기: 훅 지우기, `claude mcp remove -s user fast-question`, `~/.claude/skills` 아래 qa·qa-harvest 폴더 삭제
- 로그온할 때 웹 화면을 자동으로 띄우기(Windows 전용, 작업 스케줄러 등록): `.venv\Scripts\python.exe scripts\autostart.py --install`(`--remove` 로 해제). 다른 OS 에서는 등록하지 않고 안내만 낸다.
- 플러그인과 `scripts/install.py` 를 함께 쓰면 MCP·훅·Skill 이 두 벌 걸린다. 둘 중 하나만 쓴다.

## 로컬·원격 전환

저장소로 설치한 PC 는 MCP 와 즉석 질문 훅이 쓰는 DB 를 로컬 DB 와 원격 서버 사이에서 바꿀 수 있다. 저장소 폴더에서 실행한다(Linux 는 `.venv/bin/python scripts/install.py …`).

```powershell
.venv\Scripts\python.exe scripts\install.py --remote
.venv\Scripts\python.exe scripts\install.py --local
```

- 모드는 `%USERPROFILE%\.fast-question\credentials.json` 의 `mode` 에 적힌다. `scripts/install.py` 를 인자 없이 실행하면 지금 모드를 유지한다.
- `--remote` 는 저장된 서버 주소와 개인 토큰을 서버에서 확인한 뒤 쓴다. 처음이거나 다른 서버면 `--remote https://<주소>` 로 주고 개인 토큰을 입력한다. 토큰이 폐기·만료됐으면 `.venv\Scripts\python.exe fq_client\cli.py login` 으로 다시 로그인한다.
- `--local` 은 credentials.json 을 지우지 않는다. 주소·토큰·PC 식별자(device_id)가 남아 다시 원격으로 바꿀 때 토큰을 넣지 않는다.
- 바꾼 뒤 Claude Code 를 다시 시작하거나 `/mcp` 에서 `fast-question` 을 다시 연결한다.
- 전환은 DB 내용을 옮기지 않는다. 로컬 DB 를 서버로 옮기려면 `.venv\Scripts\python.exe scripts\push_local.py`([deploy/README.md](deploy/README.md)). 서버 DB 를 로컬에서 쓰려면 웹 화면 연결 패널의 "DB 파일 받기"로 받은 파일을 `config.json` 의 `db_path` 로 둔다.
- 플러그인은 원격 서버 전용이라 이 전환을 따르지 않는다. 로컬 DB 를 쓰려면 플러그인을 끄고(`claude plugin disable fast-question@fast-question`) 저장소로 설치한다.

## 서버 배포

원격 모드 서버를 Docker 로 띄우는 절차는 [deploy/README.md](deploy/README.md) 에 있다.
