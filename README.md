# NapCatQQ 机器人

基于 [NapCat](https://github.com/NapNeko/NapCatQQ)（QQ 协议端，Docker 部署）+ Python 标准库实现的 QQ 群机器人全家桶：

- **@指令监听**（`listener.py`）：抽签/求签/一言/打卡/权限管理等，三级权限（超管/管理员/普通人白名单）+ 防刷屏冷却
- **零点抢第一打卡**（`group_sign.py`）：NTP 校时 + 毫秒级忙等 + 上行延迟补偿，最大化抢群签到第一
- **抖音续火花**（`douyin_spark.py`）：无头 Chromium 每天 00:00:01 给抖音好友自动发消息续火花，QQ 群 @机器人 管理名单
- **抖音扫码+刷脸登录**（`douyin_qrlogin.py`）：登录态持久化，重启免重新登录（避开短信验证）
- **火花状态查询**（`spark_status.py`）：@机器人 查看火花天数/上次续火时间
- **每日一图**（`daily_image.py`）：@机器人 返图 + 本机 HTTP API

全部脚本为**纯 Python 单文件**（无框架），配置/日志/图片/脚本按目录分类，systemd 用户级服务常驻 + 开机自启。

## 环境要求与前置安装

### 需要什么环境

| 项目 | 要求 | 说明 |
|------|------|------|
| 操作系统 | Linux x86_64 / arm64（Debian / Ubuntu / 飞牛OS 等带 systemd 的发行版） | Windows/Mac 不可用（systemd 用户级服务是核心） |
| Docker | **必须** | 跑 NapCat 容器（QQ 协议端/机器人本体） |
| Python | ≥ 3.9 + pip3 | 所有脚本纯标准库 + requests/playwright |
| 内存 | ≥ 2GB 可用 | Chromium 限 128MB 堆 + 各脚本 30~96MB，很低；QQ 登录态常驻 |
| 网络 | 能访问 QQ 服务器；抖音功能需能访问 douyin.com | 国内服务器可直接用镜像源装 Docker 镜像 |
| 账号 | 一个小号 QQ + 手机抖音 | 大号有风控风险，建议小号 |

### 第 1 步：安装 Docker（容器运行时）

```bash
# 方式一（推荐, 官方一键脚本）:
curl -fsSL https://get.docker.com | sudo sh
# 方式二（Debian/Ubuntu 源）:
sudo apt install -y docker.io && sudo systemctl enable --now docker

# 当前用户免 sudo 使用 docker（装完重新登录生效）:
sudo usermod -aG docker $USER
```

### 第 2 步：安装 NapCat 容器（机器人本体，唯一必需容器）

```bash
mkdir -p napcat/config napcat/data
# 国内服务器优先用镜像源（Docker Hub 直连经常失败）:
docker run -d --name napcat --restart unless-stopped \
  -p 3000:3000 -p 3001:3001 -p 6099:6099 \
  -v $PWD/napcat/config:/app/napcat/config \
  -v $PWD/napcat/data:/app/napcat/data \
  docker.m.daocloud.io/mlikiowa/napcat-docker:latest
# 海外服务器直接: mlikiowa/napcat-docker:latest
```

| 端口 | 用途 |
|------|------|
| 3000 | OneBot HTTP API（脚本发消息走这里） |
| 3001 | OneBot WebSocket（脚本收群消息走这里） |
| 6099 | NapCat WebUI（登录 QQ 用） |

然后浏览器打开 `http://服务器IP:6099/webui` 扫码登录机器人 QQ，并在 WebUI 的网络配置里把 **HTTP(3000) 和 WS(3001) 的 token 都设为 `napcat`**（或你自定义的，与脚本配置保持一致）。

> 只需要这一个容器。其余组件（监听/打卡/续火花/每日一图）都是宿主机上的 Python systemd 服务，不需要额外容器。

### 第 3 步：Python 环境

```bash
sudo apt install -y python3 python3-pip          # 如未安装
pip3 install --user requests playwright          # 国内慢可加: -i https://pypi.tuna.tsinghua.edu.cn/simple

# 仅抖音续火花需要 Chromium（其余功能跳过这步）:
python3 -m playwright install chromium           # 约 150MB
sudo python3 -m playwright install-deps chromium # 缺系统库(libnss3等)时才需要 sudo
```

装好后运行 `bash install.sh` 会自动检测以上环境并完成剩余部署（配置 → systemd 服务 → 启动 → 开机自启）。

## 一键搭建

```bash
git clone https://github.com/YOUR_NAME/napcat-qq-bot.git
cd napcat-qq-bot
bash install.sh
```

脚本会交互式完成：装依赖 → 填 QQ 号/token → 生成配置 → 装 systemd 服务 → 可选装 NapCat 容器 → 启动 → 开机自启。

> 需要 `docker`（跑 NapCat 本体）和 `python3 + pip3`。抖音功能需要网络能访问 douyin.com。

## 手动搭建（不跑 install.sh 时）

### 1. NapCat 容器

```bash
docker run -d --name napcat --restart unless-stopped \
  -p 3000:3000 -p 3001:3001 -p 6099:6099 \
  -v $PWD/napcat/config:/app/napcat/config \
  -v $PWD/napcat/data:/app/napcat/data \
  mlikiowa/napcat-docker:latest
# 打开 http://服务器IP:6099/webui 扫码登录机器人 QQ
# WebUI 里把 HTTP(3000) 与 WS(3001) 的 token 都设为 napcat (或你自定义的)
```

### 2. Python 依赖

```bash
pip3 install --user requests playwright
python3 -m playwright install chromium        # 仅抖音功能需要
```

### 3. 改配置

- 各脚本顶部都有「配置区」：`BOT_QQ`（机器人QQ）、`SUPER_ADMIN`（你的QQ）、`WS_TOKEN/HTTP_TOKEN`（与 NapCat 一致）、群号等
- `conf/` 下运行时配置由 `*.example.json` 首次复制生成，或脚本自动生成

### 4. systemd 服务（用户级，勿用 root）

```bash
mkdir -p ~/.config/systemd/user
sed "s|/opt/napcat-qq-bot|$PWD|g" systemd/napcat-*.service > /dev/null  # 模板路径占位
for u in systemd/napcat-*.service; do
    sed "s|/opt/napcat-qq-bot|$PWD|g" "$u" > ~/.config/systemd/user/$(basename "$u")
done
systemctl --user daemon-reload
systemctl --user enable --now napcat-listener napcat-sign napcat-spark-status napcat-daily-image
# 开机自启(重启后免登录):
sudo loginctl enable-linger $USER
```

### 5. 抖音续火花（可选）

```bash
python3 scripts/douyin_qrlogin.py        # 手机抖音扫 images/douyin_qrcode.png → 确认刷脸 → 再扫刷脸码
systemctl --user enable --now napcat-douyin-spark.service
# 群里 @机器人 添加续火花 抖音号
```

## 目录结构

```
├── install.sh          一键搭建脚本
├── scripts/            全部脚本(每个文件头部即配置区, 带中文注释)
├── conf/               运行时配置(*.example.json 为模板, 真实配置自动生成且不入库)
├── logs/  images/      运行时日志与二维码/截图(自动生成)
├── systemd/            用户级服务定义(ExecStart 路径为 /opt/napcat-qq-bot 占位, 安装时自动改写)
└── README.md
```

## 日常维护

```bash
systemctl --user list-units 'napcat-*' --no-pager    # 巡检: 5 个应全 active
systemctl --user restart napcat-listener             # 改完代码必须重启
tail -f logs/listener.log                            # 实时日志
journalctl --user -u napcat-douyin-spark -n 50       # 服务级日志(排障)
python3 scripts/douyin_spark.py --check              # 检查抖音登录态
python3 scripts/spark_status.py --print              # 打印火花状态
python3 scripts/group_sign.py --test                 # 打卡真实测试(10秒后)
```

改脚本后**必须重启对应服务**，否则守护进程仍跑旧代码。

## 指令一览（群里 @机器人）

| 指令 | 权限 | 说明 |
|------|------|------|
| 抽签 / 求签 / 当前占卜 / 起卦 | 普通人(每天1次) | 占卜玩法 |
| 一言 / 今日打卡 | 普通人(每天1次) | 语录 / 连续打卡天数 |
| 每日一图 | 普通人 | 返回一张图 |
| 添加权限 QQ / 删除权限 QQ | 管理员 | 管理员增删 |
| 添加打卡群 群号 / 测试打卡 / 重启打卡 | 管理员 | 打卡管理 |
| 添加续火花 抖音号 / 删除续火花 / 续火花名单 / 立即续火花 | 管理员 | 抖音名单管理 |
| 查看火花 | 管理员 | 火花天数与状态 |
| 开启/关闭所有人、单群开关、清理日志 | 超管/管理员 | 全局控制 |

## ⚠️ 免责声明

自动化操作（自动打卡、自动发消息）存在被平台风控的风险，请仅用于**自己的账号**，频率参数已内置且可调。使用本项目产生的任何后果由使用者自行承担。
