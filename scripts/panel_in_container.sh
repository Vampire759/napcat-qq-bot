#!/bin/bash
# panel_in_container.sh — 宿主机上的监督循环:
#   保持 napcat 容器内运行 web_panel.py(端口 8099)。
#   容器重启后会自动重新拉起面板; 面板崩溃也会自动重启。
# 服务方式: napcat-panel-exec.service
set -u

# 项目在容器内的挂载点(不能放 /app 下: napcat 镜像入口会 chown -R /app)
PROJ=/srv/napcat-dokcer
SLEEP=15

while true; do
    running="$(docker inspect napcat --format '{{.State.Running}}' 2>/dev/null || true)"
    if [ "$running" = "true" ]; then
        if ! docker exec napcat pgrep -f "scripts/web_panel.py" >/dev/null 2>&1; then
            # uid 1000 = 宿主机 vampire2020: 面板写出的文件保持原属主, 也能连桥接 socket
            docker exec -d -u 1000:1000 napcat sh -c \
                "exec python3 ${PROJ}/scripts/web_panel.py --host 0.0.0.0 --port 8099 >> ${PROJ}/logs/web_panel.log 2>&1" \
                && echo "$(date '+%F %T') [panel-exec] 已拉起容器内面板" || true
        fi
        # else: 面板已在跑, 不动
    fi
    sleep "$SLEEP"
done