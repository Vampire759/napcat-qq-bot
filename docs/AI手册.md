# AI 助手 / 微信接入 / 自动登录二维码 手册

本文覆盖 5 个可选能力，全部由 `install.sh` 自动检测并部署，也可手动启用：

| 能力 | 脚本 | 服务 |
|------|------|------|
| 本地大模型推理（自动识别显卡/CPU） | `scripts/start_ai.sh` | `llama-ai.service` |
| QQ 群 AI 聊天 + 读空气 | `scripts/listener.py` | `napcat-listener.service` |
| 微信（飞牛 fnchat）AI 聊天 | `scripts/fnchat_ai.py` | `fnchat-ai.service` |
| 登录二维码自动发微信 | `scripts/qr_forward.py` | `qr-forward.service` |
| 网页管理面板（服务/脚本/AI 配置/日志） | `scripts/web_panel.py` | `napcat-web-panel.service` |

> AI 调用逻辑在 `scripts/ai_client.py`：**配置了外部 API 就先走外部 API，失败/超时/未配置自动回退本地 llama.cpp**，QQ 与微信共用。

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
  "api_enabled": true,
  "api_base_url": "",
  "api_key": "",
  "api_model": "",
  "api_timeout": 60,
  "api_fallback_local": true,
  "api_proxy": "",
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
| `api_enabled` | 外部 API 总开关（默认 `true`，但 `api_base_url` 为空时不会调用，仍是本地） |
| `api_base_url` | 外部 API 根地址（OpenAI 兼容，带不带 `/v1` 都行），如 `https://api.deepseek.com` |
| `api_key` | 外部 API 密钥，只存本地 `conf/`，不入库 |
| `api_model` | 外部 API 的模型名，如 `deepseek-chat` / `qwen-plus` / `gpt-4o-mini` |
| `api_timeout` | 外部 API 超时（秒，默认 60），超时即回退本地 |
| `api_fallback_local` | 外部失败时是否回退本地（默认 `true`；`false` = 失败直接不回复） |
| `api_proxy` | 可选 HTTP 代理（如 `http://127.0.0.1:7890`），访问境外 API 时用 |
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

### 2.6 显卡推理（GPU / Vulkan，自动识别）

`scripts/start_ai.sh` 启动时自动判断：`llama-server --list-devices` 能列出真实 GPU（CUDA / Vulkan / ROCm / Metal）就 `-ngl 999` 全量 offload，否则自动退回 CPU，无需改配置。

**当前本机实测（AMD RX 560 4GB + Vulkan 构建）**

| 项目 | GPU（Vulkan） | CPU（8 线程） |
|------|---------------|----------------|
| 524 tokens 提示词处理 | 142 t/s | 171 t/s |
| 生成速度 | 23~26 t/s | 17~18 t/s |
| 显存占用 | 模型缓冲 1440MB + KV 64MB（31/31 层在 GPU） | — |

短提示词时 GPU 首字延迟略高（首次请求有初始化开销），长对话总体更快；显存不足时 llama.cpp 会按可用显存自动缩减 offload 层数。

**AMD 显卡启用步骤（Debian/Ubuntu/飞牛OS）**

```bash
# 1) Vulkan 运行库 + 编译依赖
sudo apt install -y libvulkan1 vulkan-tools libvulkan-dev mesa-vulkan-drivers \
                    glslc spirv-headers glslang-tools

# 2) 让当前用户能访问显卡设备（renderD*），否则 Vulkan 只看到软件设备 llvmpipe
sudo usermod -aG render,video "$USER"     # 需重新登录生效
sg render -c 'vulkaninfo --summary'       # 能看到 AMD Radeon ... (RADV) 即成功

# 3) 编译 Vulkan 版 llama.cpp（独立目录，保留 CPU 版做回退）
curl -L -o /tmp/llama.tar.gz https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/heads/master
mkdir -p ~/llamacpp-src && tar -xzf /tmp/llama.tar.gz -C ~/llamacpp-src
cmake -S ~/llamacpp-src/llama.cpp-master -B ~/llamacpp-src/build-vulkan \
      -DGGML_VULKAN=ON -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build ~/llamacpp-src/build-vulkan -j "$(nproc)" --target llama-server
```

> 若系统 Vulkan 头较旧，编译报 `'LayerSettingEXT' is not a member of 'vk'`：
> 在 `ggml/src/ggml-vulkan/ggml-vulkan.cpp` 里把 `LayerSettingEXT` 那段用
> `#ifdef VK_EXT_layer_settings ... #else ... #endif` 包起来即可（本机已这样处理）。

`start_ai.sh` 会自动优先使用 `~/llamacpp-src/build-vulkan/bin/llama-server`，并通过 `sg render` 启动以取得显卡权限；检测不到 GPU 时仍用 CPU。

**强制切回 CPU**（在 `conf/ai_local.sh` 里指定旧二进制即可）：

```bash
LLAMA_BIN=$HOME/llamacpp/bin/llama-server
```

改完重启：`systemctl --user restart llama-ai`；日志在 `logs/llama-ai.log`（会打印 `使用 GPU 推理 (ngl=999) [sg render]` 或 `使用 CPU 推理`）。

### 2.7 外部 API 优先（API → 本地自动回退）

`scripts/ai_client.py` 统一负责 QQ 群与微信的模型调用，顺序是：

```
配置了外部 API？ ── 是 ──► 调外部 API ── 成功 ──► 用它的回答
      │                        │
      否                      失败/超时/空内容
      │                        ▼
      └────────────────► 本地 llama.cpp（api_fallback_local=true 时）
                              │
                          也失败 ──► 记日志，本条不回复
```

**最省事的改法：打开网页管理面板 →「AI 配置」**（见第 10 节），填地址 / Key / 模型名，保存后点「重启 listener + fnchat-ai」。也可以直接改 `conf/ai_config.json`：

```bash
cd ~/NapCatQQ-dokcer
python3 - <<'PY'
import json
p = "conf/ai_config.json"
d = json.load(open(p, encoding="utf-8"))
d.update({
    "api_enabled": True,
    "api_base_url": "https://api.deepseek.com",   # 换你家服务商的根地址
    "api_key": "sk-你的密钥",
    "api_model": "deepseek-chat",
    "api_timeout": 60,
    "api_fallback_local": True,
})
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
systemctl --user restart napcat-listener fnchat-ai
```

验证（日志里会明确写用了哪边）：

```bash
tail -n 30 logs/listener.log | grep "\[AI\]"
#   [AI] 外部 API 回答成功 (https://api.deepseek.com / deepseek-chat)
#   外部不可用时: [AI] 外部 API 调用失败 (...)：… → [AI] 回退本地模型
```

要点：

- 只填 `api_base_url` 不填 key 也会尝试（本机网关/Ollama 常常不需要 key）；
- `api_base_url` 留空 = 完全不调外部，和以前一样只用本地；
- 外部 API 失败不会让机器人沉默，除非把 `api_fallback_local` 设为 `false`；
- 想同时确认「这次是谁回答的」，用面板的「AI 测试」，会直接标出 `外部 API` 或 `本地模型`。

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

---

## 10. 网页管理面板（`scripts/web_panel.py`）

一个纯标准库的网页后台，浏览器打开 `http://服务器IP:8090` 即可，手机上也能用。

### 10.1 启动与登录

```bash
systemctl --user enable --now napcat-web-panel     # 安装/开机自启(install.sh 已自动做)
systemctl --user status napcat-web-panel --no-pager

# 访问令牌(首次启动自动生成并写入 conf/web_panel.json)
grep '"token"' conf/web_panel.json
journalctl --user -u napcat-web-panel -n 20 | grep 令牌
```

- 浏览器首次访问输入令牌即可；也可用一次性链接 `http://服务器IP:8090/?token=令牌` 直接进入。
- 令牌保存在 Cookie（30 天），换浏览器/清 Cookie 需要重新输入。
- 忘记/想换令牌：`python3 scripts/web_panel.py --reset-token` 后重启服务。

配置 `conf/web_panel.json`（不入库，`install.sh` 自动生成）：

```json
{ "host": "0.0.0.0", "port": 8090, "token": "" }
```

- `host` 改成 `127.0.0.1` 即只允许本机访问（要经 nginx 反代时用）；
- `port` 冲突就换一个（如 8091），改完重启 `napcat-web-panel`；
- `token` 留空则每次启动若仍为空会自动生成；手动填一段随机长字符串也可以。

### 10.2 能做什么

| 页签 | 功能 |
|------|------|
| 服务 | 列出全部 systemd 用户服务的 `active/enabled`，一键启动 / 停止 / 重启，看 journalctl 日志；「重启 listener + fnchat-ai」一键让 AI 配置生效 |
| AI 配置 | 快捷设置外部 API（开关 / 地址 / Key / 模型 / 超时 / 回退 / 代理）；在线编辑完整 `ai_config.json`；在线编辑人格 `personality.md` 与规则 `ai_rules.md`（热加载）；「AI 测试」直接标出本次回答来自外部 API 还是本地模型 |
| 脚本 | 列出 `scripts/` 下全部 `.py` / `.sh`，在线查看 / 编辑 / 保存（保存前自动备份到 `back/panel/`）/ 新建 / 删除 |
| 日志 | 浏览 `logs/*.log` 并查看末尾 N 行 |

> 保存脚本后仍需重启对应服务才会生效（服务页一键重启）。AI 配置只有 `personality.md` / `ai_rules.md` 是热加载，其余键要重启服务。

### 10.3 安全提示

- 面板默认监听 `0.0.0.0:8090`，靠令牌登录；**强烈建议**把令牌设长一点，或只在内网使用。
- 不要把 `conf/web_panel.json` 提交到 Git（`.gitignore` 里 `conf/*` 已排除）。
- 面板能改脚本 = 能执行任意代码，令牌等同于服务器权限，不要外传。
