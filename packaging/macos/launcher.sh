#!/bin/sh
# Finder executable. The user installs Python once; all app dependencies are offline.
set -eu
umask 077
CONTENTS=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
PROGRAM="$CONTENTS/Resources/Program"
ACTION=start
case "$(basename -- "$CONTENTS/..")" in *关闭*)
    ACTION=stop
    PROGRAM=$(CDPATH= cd -- "$CONTENTS/../../综合资料库.app/Contents/Resources/Program" && pwd -P)
    ;;
esac
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
if [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3.14 || command -v python3 || true)
fi
if [ -z "$PYTHON" ] || ! "$PYTHON" -I -B -c 'import sys,sysconfig,platform; assert sys.platform == "darwin" and sys.version_info[:2] == (3,14) and sys.version_info[:3] >= (3,14,8) and not sysconfig.get_config_var("Py_GIL_DISABLED") and not sysconfig.get_config_var("Py_DEBUG") and platform.machine() in ("x86_64","arm64")' >/dev/null 2>&1; then
    /usr/bin/osascript -e 'display dialog "请一次性安装 python.org 的标准 Python 3.14.8（或更新的 3.14 补丁版本），再打开综合资料库。" with title "综合资料库" buttons {"好"} default button "好" with icon caution' >/dev/null
    exit 1
fi
LOGROOT="$HOME/Library/Application Support/ReferenceLibrary/LauncherLogs"
mkdir -p "$LOGROOT"
if "$PYTHON" -I -B "$PROGRAM/packaging/macos/launch_macos.py" "$ACTION" >>"$LOGROOT/finder-launcher.log" 2>&1; then
    exit 0
fi
/usr/bin/osascript -e 'display dialog "资料库未能完成启动或关闭。详情已保存到本机 Library/Application Support/ReferenceLibrary 的私有日志。" with title "综合资料库" buttons {"好"} default button "好" with icon caution' >/dev/null
exit 1
