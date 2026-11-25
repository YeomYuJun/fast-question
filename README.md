# fast-question

[한국어](README.ko.md)

A Q&A knowledge base for the one-line questions and short answers that come up during work. People use it in the browser and Claude uses it over MCP, on the same database.
Use the hosted service at https://fast-question.com, or clone this repository and run it on your PC (local mode).

- Privacy Policy: https://fast-question.com/privacy/en
- Terms of Service: https://fast-question.com/terms/en

## Remote MCP connection

Connect Claude Code to the remote MCP server without installing anything.

```powershell
claude mcp add --transport http -s user fast-question https://fast-question.com/mcp
```

Open `/mcp` in Claude Code and authenticate `fast-question`. Sign in and approve in the browser, and the `kb_*` tools become available.
The server uses OAuth 2.0 with dynamic client registration. Sign-in is with a GitHub or Google account.

## Claude Code plugin

The plugin installs PC-side features on top of the remote MCP server. The PC needs uv (`uvx`).

```powershell
winget install --id astral-sh.uv -e
claude plugin marketplace add YeomYuJun/fast-question
claude plugin install fast-question@fast-question
```

What it contains

- MCP server `fast-question`: uses the `kb_*` tools of the remote server through a local proxy. File and git evidence is captured and checked on this PC.
- Skills `/fast-question:qa` (add and search) and `/fast-question:qa-harvest` (build Q&As from a project folder)
- Instant question hook (UserPromptSubmit): when you ask a question, it finds matching Q&As in the KB and adds them to that turn.
- Investigation-turn hook (Stop): when a turn ends that read files several times and wrote none, or in which `kb_search` returned nothing and files were read afterwards, or that began with a question the KB had no answer for and read files to answer it, it gives Claude the conditions for adding a fact, so that only facts that are costly to find again are added (only in project folders that are in the KB; not adding is the default).
- Use log (`%USERPROFILE%\.fast-question\use.jsonl`): written by the hooks and the MCP proxy on this PC only. It holds the id and question of each Q&A that was found, read, added or changed, the id of each Q&A whose url appeared in a response, and the search terms Claude passed to `kb_search` when it returned nothing. Your prompts are not written, and the log is not sent to the server. The hooks cite questions that were found again on several days, and terms that were searched for and missing, as grounds for deciding what to add.
- Study categories (`%USERPROFILE%\.fast-question\settings.json`): with top-level category names listed as `{"study_categories": ["Kubernetes"]}`, the MCP proxy returns the Q&As of those categories apart from the rest, under `study` in `kb_search` results. General knowledge you keep for study then stays out of searches for facts about your projects. The web screen and the instant question hook still show them, and a search that passes `category` searches that category as it is. This applies to this PC only.
- Use indicators: `fast-question stats` computes three figures from the use log. Retrieval (the share of `kb_search` calls that returned results), application (the share of found Q&As whose url appeared in a response that day), and evolution (the number of Q&As changed, and the share of missing search terms that a later addition filled). Options: `--days N` (default 30), `--since` / `--until <date>`, `--here` (this folder only), `--json`.

The MCP server, hooks and Skill commands run the PC client package `fast-question` with `uvx`. After installing, do the PC sign-in below once and restart Claude Code. Details are in [plugin/fast-question/README.md](plugin/fast-question/README.md).

## claude.ai and Claude Desktop

Add `https://fast-question.com/mcp` as a custom connector in claude.ai. The qa Skill, which holds the add and search procedure, is built as a file from this repository and uploaded to Claude.

```powershell
.venv\Scripts\python.exe scripts\build_desktop_skill.py
```

- The output is `dist/qa.skill` (a zip with `qa/SKILL.md` and `qa/rules.md`).
- It keeps the `<!-- remote -->` sections of `skill/qa` and drops the `<!-- local -->` sections. The remote server cannot read files on your PC, so the file evidence, evidence recheck and instant question hook steps are left out.
- qa-harvest is not included. It needs folders on your PC and the PC client commands.

## Tools

The MCP server exposes 21 tools. Read-only tools are marked read-only; tools that delete or overwrite data are marked destructive.

| Tool | Title | Kind |
| --- | --- | --- |
| `kb_search` | Search Q&As | read-only |
| `kb_get` | Get Q&A | read-only |
| `kb_categories` | List categories | read-only |
| `kb_cleanup_list` | List cleanup items | read-only |
| `kb_dict_get` | Get dictionary | read-only |
| `kb_evidence_spec` | Get evidence spec | read-only |
| `kb_harvest_get` | Get harvest record | read-only |
| `kb_add` | Add Q&A | write |
| `kb_add_many` | Add many Q&As | write |
| `kb_add_category` | Add category | write |
| `kb_update` | Update Q&A | write |
| `kb_restore` | Restore Q&A | write |
| `kb_rename_category` | Rename category | write |
| `kb_link` | Link Q&As | write |
| `kb_recheck` | Recheck evidence | write |
| `kb_cleanup_resolve` | Resolve cleanup item | write |
| `kb_harvest_set` | Set harvest record | write |
| `kb_archive` | Archive Q&A | destructive |
| `kb_delete_category` | Delete category | destructive |
| `kb_dict_edit` | Edit dictionary | destructive |
| `kb_evidence` | Edit evidence | destructive |

Limits, counted in weighted length (Hangul, CJK and full-width characters count 2, other characters 1)

- Question: one line, up to 300
- Answer: up to 800 and 6 lines. Each line of an answer that Claude adds is up to 240, the first line is 1-2 sentences, and the following lines are `- ` items
- Category name: up to 60 characters, two levels (top > sub)
- Remote account: 20,000 Q&As and 500 categories

`kb_add` and `kb_update` check the answer format, banned expressions (speculation, decision history, progress status) and sensitive information (secrets, personal information, IP addresses) for items Claude writes, and reject violations.

## PC sign-in

The plugin's MCP server and hooks connect to the server with a personal token. The token is issued through browser approval.

```powershell
uvx fast-question login
```

- The browser opens the server's approval screen. If it does not open, open the address printed in the terminal.
- After you sign in and approve, the token is saved in `%USERPROFILE%\.fast-question\credentials.json`. It ends if you do not approve within 5 minutes.
- Give the server address with `--url`. Without it, the address in credentials.json is used, and without that, `https://fast-question.com`.
- If there is a token for the same server, the old token is revoked on the server when the new one is issued.

## Claude on the web screen

Click the Claude icon at the top right of the hosted service screen to open a chat panel on the right. Register an Anthropic API key (a key made in the Claude Console) once, and Claude uses that key to search this DB and to add, edit, archive and organize categories.

- The key is stored encrypted on the server, and usage is billed to the key's account.
- Attaching file evidence, deleting items and categories, and bulk adding are not done in this panel.

## Local mode

Clone the repository and run the web screen and MCP server on your PC. The DB is a single SQLite file at `db_path` in `config.json`. Python 3.12 is required.

Windows (PowerShell)

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

- What `scripts/install.py` does
  - Copies the qa and qa-harvest Skills in `skill/` to `~/.claude/skills`.
  - Registers `run_mcp.py` as the MCP server `fast-question` in the Claude Code user scope (the `claude` CLI must be on PATH).
  - Registers the instant question hook (`hooks/ask_kb.py`) and the investigation-turn hook (`hooks/stop_harvest.py`) under UserPromptSubmit and Stop in `~/.claude/settings.json`. The original before the first change is kept as `settings.json.fast-question.bak`.
  - Adds the default usage guide to the DB.
  - The MCP server, hooks and Skill commands run with the repository's `.venv` python. Run it again if you move the repository.
- `run_web.py` serves the web screen at http://127.0.0.1:8765. Settings are in the repository's `config.json` (DB path, port, backup and log folders); use `--config "<another config.json>"` to change them.
- `run_mcp.py` is the stdio MCP entry point that Claude Code starts. Do not run it directly.
- Remove only the hooks: `scripts/install.py --remove-hook` (run with the same `.venv` python)
- Undo: remove the hooks, run `claude mcp remove -s user fast-question`, and delete the qa and qa-harvest folders under `~/.claude/skills`
- Start the web screen at sign-in (Windows only, registers a Task Scheduler task): `.venv\Scripts\python.exe scripts\autostart.py --install` (`--remove` to unregister). On other OSes it registers nothing and only prints instructions.
- Using the plugin and `scripts/install.py` together installs the MCP server, hooks and Skills twice. Use only one of them.

## Switching between local and remote

On a PC installed from the repository, the DB that the MCP server and the instant question hook use can be switched between the local DB and the remote server. Run this in the repository folder (on Linux, `.venv/bin/python scripts/install.py …`).

```powershell
.venv\Scripts\python.exe scripts\install.py --remote
.venv\Scripts\python.exe scripts\install.py --local
```

- The mode is written to `mode` in `%USERPROFILE%\.fast-question\credentials.json`. Running `scripts/install.py` without arguments keeps the current mode.
- `--remote` uses the saved server address and personal token after checking them with the server. The first time, or for another server, give `--remote https://<address>` and enter a personal token. If the token was revoked or expired, sign in again with `.venv\Scripts\python.exe fq_client\cli.py login`.
- `--local` does not delete credentials.json. The address, token and PC identifier (device_id) stay, so switching back to remote does not ask for the token.
- After switching, restart Claude Code or reconnect `fast-question` in `/mcp`.
- Switching does not move DB contents. To move the local DB to the server, run `.venv\Scripts\python.exe scripts\push_local.py` ([deploy/README.md](deploy/README.md)). To use the server DB locally, set the file downloaded with Export in the web screen's Connect panel as `db_path` in `config.json`.
- The plugin is for the remote server only and does not follow this switch. To use a local DB, disable the plugin (`claude plugin disable fast-question@fast-question`) and install from the repository.

## Server deployment

The procedure for running the remote-mode server with Docker is in [deploy/README.md](deploy/README.md) (Korean).

## License

MIT. See [LICENSE](LICENSE).
