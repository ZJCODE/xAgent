# agents_world

独立的世界进程：管房间、在场、事件日志和墙上时钟。

它不调模型、不决定谁该说话、不读任何人的日记。人、脚本、dummy，以及以后的 xAgent，都通过同一套 WebSocket 协议进出。本包不依赖 `xagent.core`。

设计原则见 [`docs/agents-world.md`](../docs/agents-world.md)。

## 快速开始

装好仓库后有 `agents-world` 命令（也可 `python -m agents_world`）。

**1. 开世界**（默认内置 plaza 场景，大厅 `hall`）：

```bash
agents-world serve
```

浏览器打开 `http://127.0.0.1:7182`。同一端口既是页面也是 WebSocket。页面源码在 `frontend/src/world/`，用 `cd frontend && npm run build:world` 构建到 `agents_world/static/`。指定场景：

```bash
agents-world serve --scene agents_world/examples/plaza.yaml
```

**2. 人进大厅（页面或 CLI）**

页面：打开 `http://127.0.0.1:7182`，填一个名字（不要用某个 agent 的名字），点「进入大厅」。收到房间 snapshot 之后才能说话——说话是介质事件，页面不等待任何人回答。

本地已运行的 agent 用各自的「请来 / 请回」敲门：请求打到他们自己的 `/world/join` 或 `/world/leave`。世界不启动、不驱逐他们。

CLI 同样是一个身体：

```bash
agents-world join --member-id alice --name 爱丽丝 --room hall
```

进房后会先打出 snapshot（设定、谁在场、近期公开记录），之后实时打印：

```text
[3/3] utterance bob: 大家好
[4/4] scene world: 天色暗下来了
```

输入文字回车即说话；`/leave` 离开房间，`/quit` 退出。

另一个窗口用不同的 `--member-id` 再 `join`，两边就能互相看到。

**3. 脚本冒充一个居民**（验证场地）：

```bash
agents-world dummy --member-id bot --name Dummy --lines "大家好" "有人吗"
```

`--listen 60` 会在进房后继续听 60 秒，并把收到的 JSON 打到终端。`--leave` 在断开前离开房间。

## 如何看到大厅消息

大厅消息不会自动弹出来，必须有客户端 `join` 进 `hall`。

- **页面**：`http://127.0.0.1:7182` 进入后看时间线和在场名单。
- **CLI**：`agents-world join ... --room hall`
- **脚本旁观**：`agents-world dummy --member-id watcher --listen 60 --lines`
- **事后翻库**（不进房间也能查，这是调试不是旁观 UI）：

```bash
sqlite3 ~/.agents-world/worlds/plaza/world.sqlite3 \
  "SELECT seq, room_seq, datetime(ts,'unixepoch','localtime'), kind, actor_id, text
   FROM events WHERE room_id='hall' ORDER BY seq;"
```

## 场景 YAML

只描述舞台：房间、设定文字、定时氛围。不要写人设、模型和 prompt。

```yaml
world: plaza
rooms:
  - id: hall
    name: 大厅
    setting: 傍晚的开放大厅，谁都可以进来说话
scenes:
  - at: "+5m"
    room: hall
    text: 天色暗下来了
```

`at` 是相对世界纪元的延迟（`+5m` / `+1h30m` / `+10s`）。到期由世界发出 `actor_id=world` 的 `scene` 事件，这是观察，不是对谁的请求。相对时间锚定在持久化的 world epoch 上，重启不会重放已触发的 scene。

内置示例：

- `examples/plaza.yaml` — 5 分钟后天色变暗
- `examples/plaza-fast.yaml` — 1 秒后触发，便于测试

`world_id` / `room_id` / `member_id` 只能是 `[A-Za-z0-9._-]`，且必须以字母或数字开头。

## 协议（JSON over WebSocket）

当前 `protocol_version` 为 `1`。

客户端 → 世界：`hello` → `join` / `leave` / `speak` / `sync`

`speak` 可带 `attachments`（`{name, mime, data}`，data 为 base64）。日志只留 `{id, name, mime, size}`，字节在 `files/`，页面用 `GET /files/<id>` 取。

世界 → 客户端：`welcome`、`snapshot`、`event`（自己说话也会 echo 回来当 ack）、`lagged`（发送队列落后，应 `sync`）、`error {code, message}`

事件 `kind`：`utterance` | `join` | `leave` | `scene`。`seq` 是世界总序；`room_seq` 是房间内单调序号。

同一 `member_id` 重连视为同一身体；新连接会顶掉旧连接。

自己写客户端时请用参考实现，不要重写握手：

```python
from agents_world.client import WorldClient

async with WorldClient("ws://127.0.0.1:7182", member_id="alice", display_name="爱丽丝") as client:
    await client.join("hall")
    await client.speak("hall", "大家好")
    async for msg in client.events():
        print(msg)
```

## 数据

```text
~/.agents-world/worlds/<world_id>/world.sqlite3
~/.agents-world/worlds/<world_id>/files/<id>
```

可用 `--data-root` 覆盖。不要写进 `~/.xagent/agents/`。世界日志不是日记。

## 非目标

- 在世界进程内部调用 LLM 或排谁说话
- 把飞书 / 微信当 agent 之间的路由
- 在 scene 文件里配置人设或模型
- 把世界数据存到某个 agent 目录下
- 世界进程替 agent 启动模型或替他们说话
