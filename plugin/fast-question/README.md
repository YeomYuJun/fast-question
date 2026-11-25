# fast-question Claude Code 플러그인

작업 중 생긴 질문과 짧은 답을 fast-question 지식 DB(https://fast-question.com)에 쌓고 찾는 플러그인입니다.
이 폴더는 `scripts/build_plugin.py` 가 만듭니다. 직접 고치지 말고 `skill/` 원본과 그 스크립트를 고칩니다.

## 들어 있는 것

- MCP 서버 `fast-question`: 원격 서버의 `kb_*` 도구를 그대로 씁니다. 파일·git 근거는 이 PC 에서 캡처하고 판정합니다.
- Skill `/fast-question:qa`(등록·검색), `/fast-question:qa-harvest`(프로젝트 폴더에서 Q&A 구축)
- 즉석 질문 훅: 질문하면 KB 에서 맞는 Q&A 를 찾아 그 턴에 넣습니다. 없으면 아무것도 넣지 않습니다.
- 조사 턴 수확 훅: 파일을 쓰지 않고 여러 번 읽어 답한 턴, `kb_search` 가 0건이었고 그 뒤 파일을 읽은 턴, 질문에 맞는 Q&A 가 없어 파일을 읽고 답한 턴이 끝날 때, 다시 알아내는 데 비용이 드는 사실만 등록하도록 조건을 Claude 에게 넣습니다(KB 에 있는 프로젝트 폴더에서만. 등록하지 않는 것이 기본입니다).
- 사용 기록(`~/.fast-question/use.jsonl`): 훅과 MCP 서버가 이 PC 에만 적습니다. 적는 것은 찾아진·읽힌·등록된·고쳐진 Q&A 의 id 와 질문, 응답에 주소가 붙은 Q&A 의 id, `kb_search` 가 0건일 때 Claude 가 넣은 검색어입니다. 사용자의 프롬프트는 적지 않고, 기록을 서버로 보내지 않습니다. 훅의 등록 안내가 여러 날 다시 찾아진 질문과 찾았으나 없었던 검색어를 판단 근거로 듭니다.
- 학습용 분류(`~/.fast-question/settings.json`): `{"study_categories": ["Kubernetes"]}` 처럼 대분류 이름을 적으면, MCP 서버가 `kb_search` 결과에서 그 대분류의 Q&A 를 `study` 로 따로 떼어 돌려줍니다. 공부하려고 쌓은 일반 지식이 프로젝트의 사실을 찾는 검색에 섞이지 않습니다. 웹 화면과 즉석 질문 훅은 그대로 보여 주고, `category` 를 준 검색은 그 분류를 그대로 찾습니다. 이 PC 에만 적용됩니다.
- 사용 지표: `uvx --from fast-question==0.1.5 fast-question stats` 가 사용 기록에서 세 가지를 계산합니다. 찾기(`kb_search` 중 결과가 있던 호출의 비율), 쓰기(찾아진 Q&A 중 그날 응답에 주소가 붙은 비율), 고치기(고친 Q&A 수, 0건이던 검색어 중 뒤의 등록으로 메워진 비율). `--days N`(기본 30), `--since`·`--until <날짜>`, `--here`(지금 폴더만), `--json`.

MCP·훅·Skill 명령은 PC 클라이언트 패키지 `fast-question==0.1.5` 을 `uvx` 로 실행합니다.

## 설치

1. uv 를 설치합니다(`uvx` 가 PATH 에 있어야 합니다). Windows: `winget install --id astral-sh.uv -e`
2. 플러그인을 설치합니다.
   ```
   claude plugin marketplace add YeomYuJun/fast-question
   claude plugin install fast-question@fast-question
   ```
3. 로그인합니다. 브라우저에서 승인하면 개인 토큰이 `%USERPROFILE%\.fast-question\credentials.json` 에 저장됩니다.
   ```
   uvx --from fast-question==0.1.5 fast-question login
   ```
4. Claude Code 를 다시 시작합니다(이미 열려 있으면 `/mcp` 에서 `fast-question` 을 다시 연결합니다).

## 설치 없이 쓰기

파일 근거와 즉석 질문 훅이 필요 없으면 플러그인 없이 원격 MCP 만 연결합니다.

```
claude mcp add --transport http -s user fast-question https://fast-question.com/mcp
```

## 주의

- 저장소의 `scripts/install.py` 로 설치한 적이 있으면 MCP·훅·Skill 이 두 벌 걸립니다. 둘 중 하나만 씁니다.
- 플러그인은 원격 서버 전용입니다. 이 PC 의 로컬 DB 를 쓰려면 플러그인을 끄고(`claude plugin disable fast-question@fast-question`) 저장소의 `scripts/install.py` 로 설치합니다.
- 다른 서버를 쓰려면 `uvx --from fast-question==0.1.5 fast-question login --url https://<주소>` 로 로그인합니다.
