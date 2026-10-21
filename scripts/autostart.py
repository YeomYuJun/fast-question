"""로그온할 때 web 서버를 콘솔 창 없이(pythonw) 띄우도록 Windows 작업 스케줄러에 등록한다.

OS 설정을 바꾸므로 사용자가 직접 실행한다. ONLOGON 작업 등록은 관리자 권한을 요구할 수 있다.
사용: .venv\\Scripts\\python.exe scripts\\autostart.py --install | --remove | --show
로그: data\\logs\\web.log (run_web.py 가 날짜별로 남긴다)
원격 모드 로컬 검사기를 매일 09:00 에 돌리기(작업 fast-question-check):
      .venv\\Scripts\\python.exe scripts\\autostart.py --check-install | --check-remove
Windows 전용이다. 다른 OS 에서는 안내만 내고 1 로 끝난다.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
ENTRY = ROOT / "run_web.py"
TASK = "fast-question-web"
CHECK_ENTRY = ROOT / "scripts" / "local_check.py"
CHECK_TASK = "fast-question-check"
USAGE = __doc__
IS_WINDOWS = os.name == "nt"


def install_args() -> list[str]:
    command = f'"{PYTHONW}" "{ENTRY}"'  # 경로에 공백이 있어도 되도록 둘 다 따옴표로 감싼다
    return ["schtasks", "/Create", "/TN", TASK, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", command, "/F"]


def check_install_args() -> list[str]:
    command = f'"{PYTHONW}" "{CHECK_ENTRY}"'
    return ["schtasks", "/Create", "/TN", CHECK_TASK, "/SC", "DAILY", "/ST", "09:00", "/RL", "LIMITED",
            "/TR", command, "/F"]


def run(args: list[str]) -> int:
    r = subprocess.run(args, capture_output=True, text=True, errors="replace")
    print((r.stdout + r.stderr).strip())
    if r.returncode != 0 and args[1] == "/Create":
        print("등록하지 못했다. 권한 오류라면 관리자 권한 PowerShell 에서 같은 명령을 다시 실행한다.")
    return r.returncode


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not IS_WINDOWS:
        print("자동 시작 등록은 Windows(작업 스케줄러) 전용이다. 이 OS 에서는 .venv/bin/python run_web.py 로 직접 띄운다.")
        return 1
    if argv == ["--install"]:
        if not PYTHONW.exists():
            print(f"가상환경의 pythonw 가 없다: {PYTHONW}")
            return 1
        return run(install_args())
    if argv == ["--remove"]:
        return run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    if argv == ["--check-install"]:
        return run(check_install_args())
    if argv == ["--check-remove"]:
        return run(["schtasks", "/Delete", "/TN", CHECK_TASK, "/F"])
    if argv == ["--show"]:
        return run(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"])
    print(USAGE)
    print("등록할 명령: " + subprocess.list2cmdline(install_args()))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
