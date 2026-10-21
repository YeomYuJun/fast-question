#!/bin/sh
# 서버 배포. GitHub Actions(.github/workflows/deploy.yml)가 배포 전용 SSH 키의 강제 명령으로 실행하고, 손으로 실행해도 같다.
# 저장소를 origin/main 으로 맞추고 이미지를 다시 만들어 띄운 뒤 /healthz 가 200 이 될 때까지 기다린다(3분).
# git clean 은 하지 않는다. 서버에만 있는 deploy/config.json·.env·aws.env 를 지우지 않기 위해서다.
# 본문을 함수로 감싸 끝까지 읽은 뒤 실행한다. git reset 이 실행 중인 이 파일을 바꿔도 남은 줄이 섞이지 않는다.
set -eu

main() {
    cd "$(dirname "$0")/.."
    git fetch --quiet origin main
    git reset --quiet --hard origin/main
    git log --oneline -1
    docker compose -f deploy/compose.yaml up -d --build
    i=0
    until docker compose -f deploy/compose.yaml exec -T app python -c \
        "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5).status != 200)" \
        >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 36 ]; then
            echo "healthz 가 3분 안에 200 이 되지 않았다"
            docker compose -f deploy/compose.yaml logs --tail 50 app
            exit 1
        fi
        sleep 5
    done
    docker image prune -f >/dev/null
    echo "배포 완료: $(git rev-parse --short HEAD)"
}

main "$@"
exit
