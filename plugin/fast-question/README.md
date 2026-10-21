# fast-question Claude Code 플러그인

작업 중 생긴 질문과 짧은 답을 fast-question 지식 DB(https://fast-question.com)에 쌓고 찾는 플러그인입니다.
이 폴더는 `scripts/build_plugin.py` 가 만듭니다. 직접 고치지 말고 `skill/` 원본과 그 스크립트를 고칩니다.

## 들어 있는 것

- MCP 서버 `fast-question`: 원격 서버의 `kb_*` 도구를 그대로 씁니다. 파일·git 근거는 이 PC 에서 캡처하고 판정합니다.
- Skill `/fast-question:qa`(등록·검색), `/fast-question:qa-harvest`(프로젝트 폴더에서 Q&A 구축)
- 즉석 질문 훅: 질문하면 KB 에서 맞는 Q&A 를 찾아 그 턴에 넣습니다.

MCP·훅·Skill 명령은 PC 클라이언트 패키지 `fast-question==0.1.1` 을 `uvx` 로 실행합니다.

## 설치

1. uv 를 설치합니다(`uvx` 가 PATH 에 있어야 합니다). Windows: `winget install --id astral-sh.uv -e`
2. 플러그인을 설치합니다.
   ```
   claude plugin marketplace add YeomYuJun/fast-question
   claude plugin install fast-question@fast-question
   ```
3. 로그인합니다. 브라우저에서 승인하면 개인 토큰이 `%USERPROFILE%\.fast-question\credentials.json` 에 저장됩니다.
   ```
   uvx --from fast-question==0.1.1 fast-question login
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
- 다른 서버를 쓰려면 `uvx --from fast-question==0.1.1 fast-question login --url https://<주소>` 로 로그인합니다.
