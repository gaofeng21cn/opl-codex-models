#!/bin/bash

set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
show_logs=0
telemetry=0
verify=0

for argument in "$@"; do
    case "$argument" in
        --debug)
            ;;
        --logs)
            show_logs=1
            ;;
        --telemetry)
            telemetry=1
            ;;
        --verify)
            verify=1
            ;;
        *)
            echo "未知参数: $argument" >&2
            exit 2
            ;;
    esac
done

user_name="$(/usr/bin/id -un)"
user_home_dir="$(/usr/bin/dscl . -read "/Users/$user_name" NFSHomeDirectory | /usr/bin/awk '{print $2}')"
if [[ -z "$user_home_dir" || "$user_home_dir" != /* || "$user_home_dir" == "/" ]]; then
    echo "无法确认当前用户目录，停止安装。" >&2
    exit 1
fi

# Keep the ad-hoc development build separate from the notarized install. Using
# the same path would replace the signed app and trigger Gatekeeper on the next
# launch.
app_path="$user_home_dir/Applications/CodexModelManager-Dev.app"
/usr/bin/pkill -x CodexModelManager 2>/dev/null || true
"$project_root/script/build_app.sh" --configuration debug --output "$app_path"

if (( telemetry )); then
    export OS_ACTIVITY_MODE=enable
fi
/usr/bin/open -n "$app_path"

if (( verify )); then
    launched=0
    for _ in {1..20}; do
        if /usr/bin/pgrep -x CodexModelManager >/dev/null; then
            launched=1
            break
        fi
        /bin/sleep 0.25
    done
    if (( ! launched )); then
        echo "应用未能在 5 秒内启动。" >&2
        exit 1
    fi
    test -x "$app_path/Contents/Helpers/CodexModelSync"
    echo "验证通过: $app_path"
fi

if (( show_logs )); then
    exec /usr/bin/log stream --style compact --predicate 'process == "CodexModelManager"'
fi
