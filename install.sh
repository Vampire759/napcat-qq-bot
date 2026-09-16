#!/usr/bin/env bash
# ============================================================
# NapCatQQ 机器人 一键搭建脚本
#
# 功能:
#   1. 安装 Python 依赖 (requests / playwright + chromium)
#   2. 交互式写入你的 QQ 号 / 管理员 QQ / NapCat token
#   3. 从 conf/*.example.json 生成运行时配置
#   4. 安装 systemd 用户级服务 (路径自动按实际安装目录改写)
#   5. 可选: 启动 NapCat docker 容器 / 立即启动服务 / 开机自启(linger)
#
# 用法:
#   bash install.sh              # 交互式
#   bash install.sh --no-color   # 同上
# ============================================================
set -euo pipefail

# ---------- 颜色 ----------
G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"
info()  { echo -e "${G}[INFO]${N} $*"; }
warn()  { echo -e "${Y}[提示]${N} $*"; }
abort() { echo -e "${R}[失败]${N} $*"; exit 1; }

# ---------- 项目根(脚本所在目录) ----------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BANNER='
  ██╗  ██╗ ██████╗ ██╗   ██╗    ██████╗ ███████╗ ██████╗
  ██║ ██╔╝██╔═══██╗██║   ██║    ██╔══██╗██╔════╝██╔════╝
  █████╔╝ ██║   ██║██║   ██║    ██████╔╝█████╗  ██║
  ██╔═██╗ ██║   ██║╚██╗ ██╔╝    ██╔══██╗██╔══╝  ██║
  ██║  ██╗╚██████╔╝ ╚████╔╝     ██████╔╝███████╗╚██████╗
  ╚═╝  ╚═╝ ╚═════╝   ╚═══╝      ╚═════╝ ╚══════╝ ╚═════╝  一键搭建
'
echo -e "$BANNER"

# ============================================================
# 第 0 步: 前置检查 (python3 / systemd / docker)
# ============================================================
[ "$(id -u)" -eq 0 ] && abort "请不要用 root 运行本脚本! 用普通用户运行 (服务是用户级 systemd)"
command -v python3 >/dev/null || abort "未检测到 python3, 请先安装: sudo apt install python3 python3-pip"
command -v systemctl >/dev/null || abort "未检测到 systemd, 本脚本仅支持 systemd 发行版 (Debian/Ubuntu/飞牛OS等)"

# --- Docker (跑 NapCat 容器本体, 必装) ---
if ! command -v docker >/dev/null 2>&1; then
    warn "未检测到 docker (NapCat 机器人本体的容器运行时, 必装)"
    echo "  一键安装(官方脚本):  curl -fsSL https://get.docker.com | sudo sh"
    echo "  或(Debian/Ubuntu):   sudo apt install -y docker.io && sudo systemctl enable --now docker"
    echo "  装完把当前用户加进 docker 组(免 sudo):  sudo usermod -aG docker \$USER  (重新登录生效)"
    read -r -p "现在就自动安装 docker (需要 sudo 密码)? [y/N]: " DO_DOCKER_INSTALL
    if [ "${DO_DOCKER_INSTALL:-n}" = "y" ]; then
        if curl -fsSL https://get.docker.com | sudo sh; then
            sudo usermod -aG docker "$USER" || true
            warn "docker 已装好; 若当前会话 docker 命令报权限错误, 请退出重新登录后再跑本脚本"
        else
            warn "docker 自动安装失败, 请按上面提示手动安装后重跑本脚本"
        fi
    fi
fi
info "前置检查通过 | 安装目录: $PROJECT_DIR"

# ============================================================
# 第 1 步: 安装 Python 依赖
# ============================================================
info "安装 Python 依赖 (requests, playwright)..."
pip3 install --user -q requests playwright \
  || pip3 install --user --break-system-packages -q requests playwright \
  || pip3 install --user -q -i https://pypi.tuna.tsinghua.edu.cn/simple requests playwright \
  || pip3 install --user --break-system-packages -q -i https://pypi.tuna.tsinghua.edu.cn/simple requests playwright \
  || abort "pip3 安装失败, 请手动执行: pip3 install --user requests playwright"
export PATH="$HOME/.local/bin:$PATH"
if python3 -c "import playwright" >/dev/null 2>&1; then
    info "安装 Chromium (抖音脚本用, 约 150MB, 首次较慢)..."
    python3 -m playwright install chromium || warn "chromium 安装失败, 抖音功能暂不可用, 其余功能不受影响"
    # Chromium 运行需要的系统库(libnss3/libatk 等), 缺了会报 Host system is missing dependencies
    read -r -p "自动安装 Chromium 系统依赖库(需要 sudo, 强烈建议)? [Y/n]: " DO_DEPS
    if [ "${DO_DEPS:-y}" != "n" ]; then
        sudo python3 -m playwright install-deps chromium \
          || warn "系统依赖安装失败; 若 chromium 启动报错, 手动执行: sudo python3 -m playwright install-deps chromium"
    fi
else
    warn "playwright 不可用, 跳过 chromium (仅影响抖音功能)"
fi

# ============================================================
# 第 2 步: 交互式写入 QQ / token
# ============================================================
echo ""
echo "=============================================="
echo " 基础配置 (直接回车 = 使用默认/占位, 之后可改)"
echo "=============================================="
read -r -p "机器人 QQ 号 (NapCat 登录的 QQ): " BOT_QQ_IN
read -r -p "超级管理员 QQ 号 (你自己): " SUPER_ADMIN_IN
read -r -p "NapCat API token (默认 napcat): " TOKEN_IN
BOT_QQ_IN="${BOT_QQ_IN:-1000000001}"
SUPER_ADMIN_IN="${SUPER_ADMIN_IN:-1000000002}"
TOKEN_IN="${TOKEN_IN:-napcat}"

if [ "$BOT_QQ_IN" = "1000000001" ]; then
    warn "机器人 QQ 未填写(仍为占位 1000000001), 部署后请手动改 scripts/ 内配置区"
else
    sed -i "s/1000000001/${BOT_QQ_IN}/g" scripts/*.py
    sed -i "s/1000000002/${SUPER_ADMIN_IN}/g" scripts/*.py
    info "已写入 机器人=${BOT_QQ_IN} 管理员=${SUPER_ADMIN_IN}"
fi
if [ "$TOKEN_IN" != "napcat" ]; then
    # 只替换含 TOKEN 字样行里的 "napcat", 不动服务名/镜像名等无关字样
    sed -i '/TOKEN/ s/"napcat"/"'"${TOKEN_IN}"'"/g' scripts/*.py
    info "已写入 NapCat token"
fi

# ============================================================
# 第 3 步: 生成运行时配置 (conf/*.json)
# ============================================================
mkdir -p conf logs images
for f in conf/*.example.json; do
    [ -e "$f" ] || continue
    target="conf/$(basename "$f" .example.json)"
    if [ ! -e "$target" ]; then
        cp "$f" "$target"
        info "生成 $target"
    fi
done

# ============================================================
# 第 4 步: 安装 systemd 用户级服务 (改写为实际路径)
# ============================================================
info "安装 systemd 用户级服务..."
mkdir -p "$HOME/.config/systemd/user"
for u in systemd/napcat-*.service; do
    sed "s|/opt/napcat-qq-bot|${PROJECT_DIR}|g" "$u" > "$HOME/.config/systemd/user/$(basename "$u")"
done
systemctl --user daemon-reload
info "服务定义已安装并 daemon-reload"

# ============================================================
# 第 5 步: NapCat 容器 (可选, 已有可跳过)
#   镜像优先国内源( DaoCloud ), 拉取失败自动回退 Docker Hub
# ============================================================
echo ""
NAPCAT_IMAGE_MIRROR="docker.m.daocloud.io/mlikiowa/napcat-docker:latest"
NAPCAT_IMAGE_HUB="mlikiowa/napcat-docker:latest"

run_napcat() {
    docker run -d --name napcat --restart unless-stopped \
      -p 3000:3000 -p 3001:3001 -p 6099:6099 \
      -v "$PROJECT_DIR/napcat/config:/app/napcat/config" \
      -v "$PROJECT_DIR/napcat/data:/app/napcat/data" \
      "$1"
}

if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'napcat'; then
    if docker ps --format '{{.Names}}' | grep -qx 'napcat'; then
        info "NapCat 容器已在运行"
    else
        warn "napcat 容器存在但未运行, 尝试启动: docker start napcat"
        docker start napcat || warn "启动失败, 手动执行: docker start napcat"
    fi
elif ! command -v docker >/dev/null 2>&1; then
    warn "docker 仍未安装, 跳过 NapCat 容器; 装好 docker 后手动执行第 5 步提示的命令"
else
    warn "未检测到 napcat 容器(机器人本体)。将执行:"
    echo "  docker run -d --name napcat --restart unless-stopped \\"
    echo "    -p 3000:3000 -p 3001:3001 -p 6099:6099 \\"
    echo "    -v $PROJECT_DIR/napcat/config:/app/napcat/config \\"
    echo "    -v $PROJECT_DIR/napcat/data:/app/napcat/data \\"
    echo "    $NAPCAT_IMAGE_MIRROR   (失败自动换 Docker Hub 源)"
    echo "装好后打开 http://服务器IP:6099/webui 扫码登录机器人 QQ, 并把 WebUI 里"
    echo "OneBot 的 HTTP(3000)/WS(3001) token 设为上面填的 token。"
    read -r -p "现在就执行上面命令安装 NapCat? [y/N]: " DO_DOCKER
    if [ "${DO_DOCKER:-n}" = "y" ]; then
        run_napcat "$NAPCAT_IMAGE_MIRROR" \
          || run_napcat "$NAPCAT_IMAGE_HUB" \
          || warn "容器启动失败(网络/权限), 可稍后手动执行上面命令"
    fi
fi

# ============================================================
# 第 6 步: 启动服务
# ============================================================
echo ""
read -r -p "立即启动 4 个基础服务(监听/打卡/火花状态/每日一图)? [Y/n]: " DO_START
if [ "${DO_START:-y}" != "n" ]; then
    systemctl --user enable --now napcat-listener.service napcat-sign.service \
              napcat-spark-status.service napcat-daily-image.service
    systemctl --user list-units 'napcat-*' --no-pager | sed -n '1,8p'
fi

# ============================================================
# 第 7 步: 开机自启 (linger)
# ============================================================
echo ""
warn "开机自启最后一步: 让用户服务在未登录时也运行(linger)"
read -r -p "现在执行 loginctl enable-linger? [Y/n]: " DO_LINGER
if [ "${DO_LINGER:-y}" != "n" ]; then
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" \
        || warn "开启失败, 稍后手动执行: sudo loginctl enable-linger $USER"
    loginctl show-user "$USER" -p Linger || true
fi

# ============================================================
# 完成
# ============================================================
echo ""
info "=============================================="
info " 搭建完成! 接下来:"
info " 1. 抖音续火花(可选): python3 scripts/douyin_qrlogin.py"
info "    手机抖音扫 images/douyin_qrcode.png 并完成刷脸, 然后:"
info "    systemctl --user enable --now napcat-douyin-spark.service"
info " 2. 群里 @机器人 添加续火花 抖音号 / 添加打卡群 群号"
info " 3. 日常维护命令见 README.md"
info "=============================================="
