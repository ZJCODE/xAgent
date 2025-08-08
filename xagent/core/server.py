import os
import yaml
import uvicorn
import argparse
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv

from xagent.core.agent import Agent
from xagent.core.session import Session
from xagent.db.message import MessageDB
from xagent.tools import TOOL_REGISTRY

# 1. 加载 .env（密钥等）
load_dotenv(override=True)

# 2. 加载 YAML 配置（可指定路径）
def load_config(cfg_path):
    if not os.path.isfile(cfg_path):
        # 支持相对路径查找
        base = os.path.dirname(os.path.abspath(__file__))
        abs_path = os.path.join(base, cfg_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Cannot find config file at {cfg_path} or {abs_path}")
        cfg_path = abs_path
    with open(cfg_path, "r") as f:
        return yaml.safe_load(f)

# 3. 命令行参数
def get_args():
    parser = argparse.ArgumentParser(description="xAgent HTTP Server")
    parser.add_argument('--config', type=str, default="config.yaml", help="Path to config yaml file")
    return parser.parse_args()

args = get_args()
config = load_config(args.config)

# 4. 初始化 agent
agent_cfg = config["agent"]

tool_names = agent_cfg.get("tools", [])
tools = [TOOL_REGISTRY[name] for name in tool_names if name in TOOL_REGISTRY]


# 根据 use_local_session 参数决定是否使用 MessageDB
use_local_session = agent_cfg.get("use_local_session", True)
message_db = None if use_local_session else MessageDB()

agent = Agent(
    name=agent_cfg.get("name"),
    system_prompt=agent_cfg.get("system_prompt"),
    model=agent_cfg.get("model"),
    tools=tools,
    mcp_servers=agent_cfg.get("mcp_servers"),
)

app = FastAPI(title="xAgent HTTP Agent Server")

# 5. 定义请求体
class AgentInput(BaseModel):
    user_id: str
    session_id: str
    user_message: str
    image_source: str = None

# 6. 路由
@app.post("/chat")
async def chat(input: AgentInput):
    session = Session(user_id=input.user_id, session_id=input.session_id, message_db=message_db)
    response = await agent(
        user_message=input.user_message,
        session=session,
        image_source=input.image_source
    )
    return {"reply": str(response)}

# 7. 启动
if __name__ == "__main__":
    server_cfg = config.get("server", {})
    uvicorn.run(
        "xagent.core.server:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 8010),
        reload=server_cfg.get("debug", False),
    )