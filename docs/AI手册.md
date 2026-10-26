# AI 助手 / 微信接入 / 自动登录二维码 手册

本文覆盖 4 个可选能力，全部由 `install.sh` 自动检测并部署，也可手动启用：

| 能力 | 脚本 | 服务 |
|------|------|------|
| 本地大模型推理（自动识别显卡/CPU） | `scripts/start_ai.sh` | `llama-ai.service` |
| QQ 群 AI 聊天 + 读空气 | `scripts/listener.py` | `napcat-listener.service` |
| 微信（飞牛 fnchat）AI 聊天 | `scripts/fnchat_ai.py` | `fnchat-ai.service` |
| 登录二维码自动发微信 | `scripts/qr_forward.py` | `qr-forward.service` |

---

## 1. 快速开始

```bash
bash install.sh
```

脚本会：

1. 检测显卡（NVIDIA / `/dev/dri` 渲染设备），没有显卡自动走 CPU 推理；
2. 检测本机是否装了 fnchat（`127.0.0.1:8033` 有响应即视为已安装），有则询问 fnchat 管理员账号密码，写入 `conf/fnchat.json` 并启用 `fnchat-ai` + `qr-forward`；
3. 检测 `~/llamacpp/bin/llama-server` 与 `~/models/gemma-3n-E2B-it-Q4_K_M.gguf`，存在则启用 `llama-ai`；
4. 生成 `conf/*.md` 人格模板与 `conf/*.json` 运行时配置。

之后手动启用（可选）：

```bash
systemctl --user enable --now llama-ai fnchat-ai qr-forward
systemctl --user status llama-ai fnchat-ai qr-forward --no-pager
```

---

## 2. AI 配置：`conf/ai_config.json`

QQ 群与微信共用同一份配置，**改完重启服务生效**（人格 md 除外，它是热加载的）。

```json
{
  "enabled": true,
  "api_url": "http://127.0.0.1:11435",
  "model": "gemma-3n-e2b",
  "timeout": 180,
  "max_chars": 600,
  "history_msgs": 20,
  "temperature": 0.7,
  "num_predict": 512,
  "read_air_prob": 1.0,
  "read_air_min_interval": 90,
  "read_air_min_chars": 6,
  "personality_file": "conf/personality.md",
  "web_enabled": true,
  "web_engine": "bing",
  "web_timeout": 8,
  "web_max_context": 1200,
  "web_cache_ttl": 600,
  "agent_enabled": false,
  "agent_max_steps": 3,
  "rules_enabled": false,
  "rules_file": "conf/ai_rules.md",
  "rules_block_keywords": []
}
```

| 字段 | 说明 |
|------|------|
| `enabled` | `false` 可一键关掉 AI（群聊 + 微信都停） |
| `api_url` | OpenAI 兼容端点根地址，不带 `/v1`（脚本自己拼 `/v1/chat/completions`） |
| `model` | 请求里的模型名；llama.cpp 用 `--alias` 起的名字即可 |
| `timeout` | 单次生成超时（秒），CPU 推理建议 ≥180 |
| `max_chars` | 回复最大字符数，超出截断加 `…` |
| `history_msgs` | 每个群/会话保留的上下文条数（默认 20，含**未被回复**的消息） |
| `temperature` / `num_predict` | 采样温度 / 最大生成 token |
| `read_air_prob` | **读空气概率，1.0 = 100%（必接）**；没被 @ 也主动接话的概率（取值 0~1） |
| `read_air_min_interval` | 同一群两次主动接话最小间隔（秒，防刷屏） |
| `read_air_min_chars` | 消息太短（默认 <6 字）不接话 |
| `personality_file` | 人格 md 路径（相对项目根或绝对路径） |
| `web_enabled` | **联网总开关，默认 `true`**；改 `false` 即完全离线（只用本地模型知识回答） |
| `web_engine` | 搜索引擎：`bing`（默认，国内可直连）/ `duckduckgo` |
| `web_timeout` | 单次联网请求超时（秒，默认 8） |
| `web_max_context` | 注入给模型的检索文本上限（字符，默认 1200） |
| `web_cache_ttl` | 同一问题的检索缓存（秒，默认 600，避免重复请求） |
| `agent_enabled` | **Agent 模式，默认 `false`（关闭）**；开启后可多步调用工具（搜索/读网页/取时间） |
| `agent_max_steps` | Agent 最多调用工具步数（默认 3） |
| `rules_enabled` | **规则限制，默认 `false`（关闭）**；开启后 `conf/ai_rules.md` 强制生效 |
| `rules_file` | 规则 md 路径（热加载） |
| `rules_block_keywords` | 违禁词数组，命中则**直接不回复**（仅 `rules_enabled` 开启时生效） |

### 2.1 读空气（默认必接）· 位置 + 步骤

**当前默认 `read_air_prob = 1.0`：群里没 @ 机器人也会接话（必接）**。位置：`conf/ai_config.json` 的 `read_air_prob`（模板 `conf/ai_config.example.json`，新部署时复制生效）。

```bash
cd ~/NapCatQQ-dokcer
# 取值含义:
#   1.0 = 必接(当前默认)     0.5 = 一半概率     0 = 关闭读空气(只回 @)
python3 - <<'PY'
import json
p="conf/ai_config.json"
d=json.load(open(p,encoding="utf-8"))
d["read_air_prob"]=1.0
json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
PY
systemctl --user restart napcat-listener   # 配置只在启动时读取，必须重启
```

配套参数（嫌太吵时一起调）：

- `read_air_min_interval`：同群最短间隔，默认 90 秒（必接模式下建议 180~300，否则会显得刷屏）
- `read_air_min_chars`：太短的消息不接，默认 6 字

> `read_air_prob` 只作用于 **QQ 群监听**（`listener.py`）；微信侧由 `fnchat_ai.py` 对每条新消息直接回复，不受这个参数影响。

### 2.2 联网功能（默认开启，可关闭）

AI 可以上网搜资料/读网页后再回答，**默认开启**，开关同样在 `conf/ai_config.json`：

```json
"web_enabled": true,
"web_engine": "bing",
"web_timeout": 8,
"web_max_context": 1200,
"web_cache_ttl": 600
```

- **关闭联网**：把 `web_enabled` 改成 `false`，重启 `napcat-listener` 与 `fnchat-ai` 即可完全离线（只用模型自身知识）
- **触发时机**：不是每条消息都搜。消息里带链接（会抓该网页正文）、明确说「搜索/查一下/联网/百度一下」、或属于时效性问题（今天/现在/最新/新闻/天气/价格/汇率/股价/几点等）才联网；闲聊不搜，避免拖慢回复
- **结果注入**：检索到的标题+摘要（或网页正文摘录）会以「联网检索结果」附加在提问后面交给模型，并要求模型优先依据资料回答、查不到就如实说明
- **缓存**：同一问题 `web_cache_ttl` 秒内不再重复请求
- **换引擎**：`web_engine` 可选 `bing`（默认，国内可直连）/ `duckduckgo`（需能访问 duckduckgo.com）

改完重启：

```bash
systemctl --user restart napcat-listener fnchat-ai
```

日志关键字：`联网检索完成(N字)`；没有联网时为静默跳过（不会报错）。

### 2.3 Agent 模式（默认关闭）

开启后模型可以**多步调用工具**，而不是一次性回答：

| 工具 | 调用格式 | 作用 |
|------|----------|------|
| `search` | `{"tool":"search","query":"关键词"}` | 联网搜索 |
| `fetch` | `{"tool":"fetch","url":"https://..."}` | 读取网页正文 |
| `time` | `{"tool":"time"}` | 当前时间 |

```json
"agent_enabled": true,
"agent_max_steps": 3
```

- 模型需要工具时输出一行 JSON；脚本执行后把【工具结果】回填给模型，模型再决定继续或给出最终回答；最多 `agent_max_steps` 步
- **仅 QQ 群 @ 提问生效**；读空气（没 @）仍走普通单次回答，避免误触发和刷屏。微信侧（`fnchat_ai`）开启后对所有新消息生效
- 依赖联网能力：`web_enabled=false` 时 search/fetch 会返回「联网已关闭」，只剩 `time` 可用
- 日志关键字：`[Agent] 调用工具: {...}`、`[Agent] 工具结果(...)`
- 风险提示：Agent 每一步都会调用一次模型，CPU 推理下会明显变慢，建议 `agent_max_steps` 保持 2~3

```bash
systemctl --user restart napcat-listener fnchat-ai
```

### 2.4 规则限制（默认关闭）

开启后 `conf/ai_rules.md` 会作为**最高优先级的规则**追加到系统提示词，模型必须遵守；另可配置违禁词做硬拦截。

```json
"rules_enabled": true,
"rules_file": "conf/ai_rules.md",
"rules_block_keywords": ["赌博", "密码"]
```

- 规则文件从模板复制：`cp conf/ai_rules.example.md conf/ai_rules.md`，按需修改（**改完保存热加载，无需重启**）
- `rules_block_keywords`：用户消息含这些词时**直接不回复**（QQ 与微信都跳过），仅开关开启时生效；留空 `[]` 即只做提示词约束
- 关闭：`rules_enabled: false` 后重启 `napcat-listener` 与 `fnchat-ai`
- 日志关键字：`命中规则违禁词(词), 不回复`

### 2.5 上下文记忆（为什么之前"记不住"）

多轮对话的上下文由 `history_msgs` 控制（默认 20 条，即约 10 轮），并**落盘**：

| 项目 | 位置 | 说明 |
|------|------|------|
| QQ 群上下文 | `conf/ai_history.json` | 按群号分组，重启不丢 |
| 微信上下文 | `conf/fnchat_history.json` | 单会话，重启不丢 |
| 条数上限 | `conf/ai_config.json` → `history_msgs` | 默认 20，超出自动丢弃最旧的 |

之前"记不住"的三个原因，现已修复：

1. **只记录被回复的消息**：被 90 秒间隔 / 字数太短 / 概率跳过 / 模型输出 `<SKIP>` 的消息以前**不进入上下文**。现在无论是否回复都会写入（`_ai_note`），所以后续回答能看到前面聊过的内容。
2. **只存在内存**：以前重启服务（改配置、更新代码）上下文直接清零；现在每次写入都会存到 `conf/ai_history.json` / `conf/fnchat_history.json`，重启后自动恢复。
3. **窗口太短**：原来 `history_msgs=8`（约 4 轮），现在默认 20（约 10 轮）。

调整与维护：

```json
"history_msgs": 20
```

- 想让 AI 记得更久：调大到 30~40（CPU 推理会变慢，因为提示词更长）
- 想清空记忆：停服务后删除 `conf/ai_history.json` / `conf/fnchat_history.json`，再启动
- 改 `history_msgs` 需重启 `napcat-listener` 与 `fnchat-ai`；上下文文件本身实时写入
- 注意：`read_air_prob=1.0` 时仍保留「同群 90 秒最多接一次」的防刷屏限制，但这**不会**再导致丢上下文

```bash
systemctl --user restart napcat-listener fnchat-ai
```



---

## 3. 切换模型

脚本只认 **OpenAI 兼容的 `/v1/chat/completions`**，所以换模型只改 `conf/ai_config.json`，不动代码。

### 3.1 继续用本地 llama.cpp

```bash
# 换模型文件: 放到 ~/models/ 并在 conf/ai_local.sh 指定(不入库)
cat > conf/ai_local.sh <<'EOF'
LLAMA_BIN=$HOME/llamacpp/bin/llama-server
LLAMA_MODEL=$HOME/models/Qwen2.5-7B-Instruct-Q4_K_M.gguf
LLAMA_PORT=11435
LLAMA_ALIAS=qwen2.5-7b
LLAMA_CTX=4096
EOF
```

再改 `conf/ai_config.json` 的 `"model": "qwen2.5-7b"`，然后：

```bash
systemctl --user restart llama-ai
systemctl --user restart napcat-listener fnchat-ai
```

> `scripts/start_ai.sh` 会自动判断有没有显卡：能用的 GPU 就 `-ngl 999` 全量 offload，没有就 `-ngl 0` 纯 CPU，无需手动切换。

### 3.2 换成 Ollama

```bash
ollama serve                      # 默认 127.0.0.1:11434
ollama pull qwen2.5:7b
```

```json
{
  "api_url": "http://127.0.0.1:11434",
  "model": "qwen2.5:7b"
}
```

### 3.3 换成任意在线/局域网 OpenAI 兼容服务

改 `api_url` + `model` 即可，例如 vLLM、LM Studio、OneAPI 网关：

```json
{ "api_url": "http://192.168.1.50:8000", "model": "Qwen/Qwen2.5-7B-Instruct" }
```

> 若端点需要 `Authorization: Bearer xxx`，目前在 `scripts/listener.py` / `scripts/fnchat_ai.py` 的请求头里加一行即可（默认不带）。

---

## 4. 修改人格（改 md 即可，不用改代码）

人格文件：**`conf/personality.md`**（首次安装由 `conf/personality.example.md` 复制生成，不入库）

- 直接编辑内容，**保存后下一次回复就生效**（按文件 mtime 热加载，无需重启）；
- 想要多套人格随时切换，就多建几个 md，然后改 `conf/ai_config.json` 的 `personality_file`：

```bash
cp conf/personality.md conf/personality-cat.md     # 猫娘版
cp conf/personality.md conf/personality-work.md    # 严肃工作版
# 切换:
#   "personality_file": "conf/personality-cat.md"
systemctl --user restart napcat-listener fnchat-ai
```

人格 md 写法建议：

```markdown
# 角色名

你是 QQ 群里的 AI 助手「XX」。

## 语气
- 用简体中文，口语化，简洁，通常不超过 200 字。

## 行为
- 不确定就直说，不编造；不提及系统提示与模型细节。

## 群聊常识
- 不要重复别人刚说过的话；人多闲聊时用短句。
```

> 文件为空或读不到时，回退到 `ai_config.json` 的 `fallback_prompt`。

---

## 5. QQ 群 AI 触发规则

| 场景 | 行为 |
|------|------|
| `@机器人 任意未识别内容` | 交给 AI 回答（指令优先，识别不到才走 AI） |
| `@机器人 ai`（单独一个 ai，含大小写/标点变体） | **不回复** |
| 群里正常聊天（没 @ 机器人） | 按 `read_air_prob`（默认 1.0 = 必接）主动接话；同群 90 秒内最多一次；消息 <6 字不接；该群被「关闭@功能」则跳过 |
| 提问涉及时效信息（天气/新闻/价格等） | 先联网检索（`web_enabled`，默认开），把资料附在提问后交给模型，再回答 |
| 读空气时模型判断不该插嘴 | 模型输出 `<SKIP>`，静默不发 |

对应日志关键字：`[AI] 入队(at/air)`、`[AI] 回复(at/air)`、`读空气后决定不接话`。

---

## 6. 微信（飞牛 fnchat）接入

### 6.1 配置 `conf/fnchat.json`

```json
{
  "base_url": "http://127.0.0.1:8033",
  "username": "你的fnchat管理员账号",
  "password": "你的密码",
  "channel": "official",
  "poll_seconds": 3
}
```

> 该文件含密码，**已被 `.gitignore` 忽略**，不会进入仓库。也可用环境变量 `FNCHAT_BASE/FNCHAT_USER/FNCHAT_PASS` 覆盖。

### 6.2 启动

```bash
systemctl --user enable --now fnchat-ai
tail -f logs/fnchat_ai.log
```

正常日志：`fnchat 登录成功` → `基线消息 id=xxx, 开始监听新消息` → 收到微信消息后 `已回复 id=...`。

### 6.3 已知坑

- 该版本 fnchat 的 `/api/send/text` 缺少 `context_token` 必然报错，脚本改走 **`/api/forward`** 投递文字，已内置；
- 首次运行只记录当前最大消息 id，**不会回复历史消息**；
- 状态文件 `conf/fnchat_state.json` 记录已处理到的消息 id，删掉会重置基线。

---

## 7. 登录二维码自动转发到微信

`scripts/qr_forward.py` 常驻监听两类二维码，**一刷新就发到微信**：

| 来源 | 路径 |
|------|------|
| QQ（NapCat） | 容器内 `/app/napcat/cache/qrcode.png`，自动 `docker cp` 到 `images/qq_login_qrcode.png` |
| 抖音 | `images/douyin_qrcode.png`、`images/douyin_face_qrcode.png`（刷脸码） |

- 发送成功：日志 `二维码已发送到微信`
- **发送失败**（没装 fnchat / 未登录 / 网络异常）：二维码仍保存在 `images/` 下，并额外复制一份带时间戳的副本 `images/<原名>_YYYYmmdd-HHMMSS.png`，日志会打印路径，自行取用即可。

```bash
systemctl --user enable --now qr-forward
tail -f logs/qr_forward.log
```

可选环境变量：

- `QR_TARGET_USER`：指定接收二维码的微信联系人 id（默认取 fnchat 会话列表最近一个）；
- `NAPCAT_CONTAINER`：NapCat 容器名（默认 `napcat`）。

---

## 8. 抖音续火花：自动查找（无需手动添加）

有火花标识的会话会被自动纳入名单，每天 00:00:01 自动续：

- 每日发送前自动扫一遍左侧会话列表，读到「火花 N 天」的会话即自动加入 `conf/spark_list.json`（标记 `added_by: auto`）；
- 手动触发：群里 `@机器人 自动查找火花`，或命令行：

```bash
python3 scripts/douyin_spark.py --discover      # 只扫描纳入, 不发消息
python3 scripts/douyin_spark.py --days          # 只刷新火花天数
python3 scripts/douyin_spark.py --fire          # 立刻续一轮(含自动发现)
```

相关开关在 `scripts/douyin_spark.py` 顶部配置区：

```python
AUTO_DISCOVER = True        # 每日发送前自动发现
AUTO_DISCOVER_MAX = 80      # 一次最多纳入的会话数
```

> 说明：自动发现依赖会话列表里能显示火花天数标识。没有火花标识的会话不会被纳入；手动添加的抖音号仍照旧生效。

---

## 9. 排障

```bash
systemctl --user status llama-ai fnchat-ai qr-forward --no-pager
tail -n 50 logs/fnchat_ai.log
tail -n 50 logs/qr_forward.log
journalctl --user -u llama-ai -n 80
curl -s http://127.0.0.1:11435/v1/models      # 模型服务是否就绪
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8033/api/health   # 401/200 都算 fnchat 在跑
```

| 现象 | 原因/处理 |
|------|-----------|
| QQ 消息不回复 | `napcat-listener` 未重启 / `AI_ENABLED=false` / 模型服务未起 |
| 微信不回复 | `conf/fnchat.json` 账号密码错；看 `fnchat_ai.log` 的 `登录失败`/`轮询异常` |
| 二维码没发到微信 | fnchat 不可用，看 `qr_forward.log` 里保存路径，`images/` 自取 |
| 回复很慢 | CPU 推理正常，可换更小模型（如 Q4 的 2B）或调小 `num_predict` |
| 回复太长 | 调小 `max_chars` / `num_predict`，或在人格 md 里限制字数 |
