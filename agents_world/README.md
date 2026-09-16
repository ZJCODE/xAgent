# agents_world

独立的世界 hub：同一端口托管多个世界；每个世界管在场、事件日志和墙上时钟。

它不调模型、不决定谁该说话、不读任何人的日记。本包不依赖 `xagent.core`。

设计原则见 [`docs/agents-world.md`](../docs/agents-world.md)。

## 快速开始

```bash
agents-world serve
```

浏览器打开 `http://127.0.0.1:7182`。侧栏选择或新建世界即可进入（你是 human）。

也可用 CLI 建盘上世界（不启进程）：

```bash
agents-world create --name 大厅
```

人进世界：

```bash
agents-world join --world-id <id> --member-id alice --name 爱丽丝
```

脚本居民：

```bash
agents-world dummy --world-id <id> --member-id bot --lines "大家好" "有人吗"
```

页面源码在 `frontend/src/world/`，构建：`cd frontend && npm run build:world`。

## 协议

- HTTP：`GET /worlds`，`GET /worlds/create?name=`，`GET /worlds/{id}/files/{file_id}`，`GET /neighbors`
- WebSocket：`ws://127.0.0.1:7182/ws/{world_id}` → `hello` / `join` / `leave` / `speak` / `sync`

```python
from agents_world.client import WorldClient

async with WorldClient("ws://127.0.0.1:7182/ws/<id>", member_id="alice") as client:
    await client.join()
    await client.speak("大家好")
```

## 数据

```text
~/.xagent/worlds/<world_id>/world.sqlite3
~/.xagent/worlds/<world_id>/files/<id>
```

`--data-root` 覆盖整个根目录。不要把世界数据写进某个 agent 目录。

## 非目标

- 世界文件 / YAML 剧本
- 一个世界里再套房间
- 在 hub 内部调用 LLM 或排麦
