# systemd 用户级服务

本目录 5 个 `.service` 是**备份源**，`ExecStart` 里的 `/opt/napcat-qq-bot` 是占位路径，
由 `install.sh` 安装时自动改写为实际目录后拷贝到 `~/.config/systemd/user/`。

## 服务清单

| 服务 | 脚本 | 作用 |
|------|------|------|
| napcat-listener.service | scripts/listener.py | 群监听 + @指令 |
| napcat-sign.service | scripts/group_sign.py | 零点抢第一打卡 |
| napcat-douyin-spark.service | scripts/douyin_spark.py | 抖音续火花(00:00:01) |
| napcat-spark-status.service | scripts/spark_status.py | @查看火花 |
| napcat-daily-image.service | scripts/daily_image.py | 每日一图 |

## 手动安装 / 恢复

```bash
mkdir -p ~/.config/systemd/user
for u in systemd/napcat-*.service; do
    sed "s|/opt/napcat-qq-bot|$PWD|g" "$u" > ~/.config/systemd/user/$(basename "$u")
done
systemctl --user daemon-reload
systemctl --user enable --now napcat-listener napcat-sign napcat-spark-status napcat-daily-image
# 抖音脚本需先扫码登录: python3 scripts/douyin_qrlogin.py
systemctl --user enable --now napcat-douyin-spark
# 开机自启(重启后免登录):
sudo loginctl enable-linger $USER
```

## 常用命令

```bash
systemctl --user list-units 'napcat-*' --no-pager     # 巡检
systemctl --user restart napcat-listener.service      # 改代码后重启
journalctl --user -u napcat-listener.service -f       # 服务日志
tail -f logs/listener.log                             # 脚本日志
loginctl show-user $USER -p Linger                    # 应为 Linger=yes
```
