import sys
import os
import time
from typing import Optional
import streamlit as st
import asyncio
import re
import tempfile


# 添加项目根目录到 Python 路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.core import Session, Agent
from xagent.db import MessageDB
from tools.vocabulary_tool import lookup_word, get_vocabulary
from tools.openai_tool import web_search,draw_image

# 页面配置
st.set_page_config(
    page_title="对话测试",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 初始化 Session State
def init_session_state():
    """初始化 Streamlit session state"""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    if "agent" not in st.session_state:
        st.session_state.agent = None
    
    if "session" not in st.session_state:
        st.session_state.session = None
    
    if "user_id" not in st.session_state:
        st.session_state.user_id = "streamlit_user"
    
    if "session_id" not in st.session_state:
        st.session_state.session_id = "test_session"  # 可以设置为 None 或空字符串
    
    if "use_redis" not in st.session_state:
        st.session_state.use_redis = True
    
    if "show_image_upload" not in st.session_state:
        st.session_state.show_image_upload = False

def create_agent_and_session(user_id: str, session_id: Optional[str], use_redis: bool, model: str):
    """创建 Agent 和 Session 实例"""
    # 创建工具列表

    story_agent = Agent(system_prompt="you are a story maker who can tell vivid stories.",
                        model="gpt-4.1-mini")
    
    story_tool = story_agent.as_tool(name="story_make_tool", description="A tool to tell stories based on user input and return the story for reference.")

    tools = [lookup_word, get_vocabulary, web_search, draw_image, story_tool]

    # 创建 Agent
    agent = Agent(model=model, 
                  tools=tools,
                  mcp_servers=["http://127.0.0.1:8001/mcp/"],
                  system_prompt=f"Current date is {time.strftime('%Y-%m-%d')}")

    # 创建 Session
    message_db = MessageDB() if use_redis else None
    session = Session(
        user_id=user_id,
        session_id=session_id,
        message_db=message_db
    )
    
    return agent, session

def render_markdown_with_img_limit(content: str, max_width: int = 400):
    """
    将 markdown 图片语法替换为带最大宽度限制的 HTML img 标签
    """
    def replacer(match):
        alt = match.group(1)
        url = match.group(2)
        return f'<img src="{url}" alt="{alt}" style="max-width:{max_width}px;">'
    # 匹配 ![alt](url)
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    return re.sub(pattern, replacer, content)

def display_chat_history():
    """显示聊天历史"""
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            content = message["content"]
            # 判断是否为 base64 图片 markdown
            if isinstance(content, str) and content.startswith("![generated image](data:image/png;base64,"):
                prefix = "![generated image]("
                suffix = ")"
                img_url = content[len(prefix):-len(suffix)]
                st.markdown(
                    f'<img src="{img_url}" style="max-width:400px;">',
                    unsafe_allow_html=True
                )
            else:
                # 新增：对所有 markdown 内容做图片宽度限制
                st.markdown(render_markdown_with_img_limit(content), unsafe_allow_html=True)
            if "timestamp" in message:
                st.caption(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(message['timestamp']))}")

def main():
    """主函数"""
    init_session_state()
    
    # 侧边栏配置
    with st.sidebar:
        st.title("对话配置")
        
        # 用户配置
        st.subheader("用户设置")
        user_id = st.text_input("用户ID", value=st.session_state.user_id, key="user_id_input")
        session_id = st.text_input("会话ID (可选)", value=st.session_state.session_id or "", key="session_id_input")
        
        # 存储配置
        st.subheader("存储设置")
        use_redis = st.checkbox("使用 Redis 存储", value=st.session_state.use_redis)
        
        # 模型配置
        st.subheader("模型设置")
        model_options = ["gpt-4o-mini", "gpt-4o", "gpt-4.1"]
        model = st.selectbox("选择模型", model_options, index=2)
        
        # 新增：图片上传模块显示控制
        st.subheader("界面设置")
        show_image_upload = st.checkbox("显示图片上传模块", value=st.session_state.show_image_upload)
        if show_image_upload != st.session_state.show_image_upload:
            st.session_state.show_image_upload = show_image_upload
            st.rerun()
        
        # 应用配置按钮
        if st.button("应用配置", type="primary"):
            st.session_state.user_id = user_id
            st.session_state.session_id = session_id if session_id else None
            st.session_state.use_redis = use_redis
            
            # 重新创建 Agent 和 Session
            try:
                agent, session = create_agent_and_session(
                    user_id, 
                    st.session_state.session_id, 
                    use_redis, 
                    model
                )
                st.session_state.agent = agent
                st.session_state.session = session
                st.success("配置已应用！")
            except Exception as e:
                st.error(f"配置失败: {str(e)}")
        
        # 清空历史按钮
        if st.button("清空对话历史", type="secondary"):
            if st.session_state.session:
                st.session_state.session.clear_session()
                st.session_state.messages = []
                st.success("对话历史已清空！")
                st.rerun()
        
        # 显示当前配置
        st.subheader("当前配置")
        st.write(f"**用户ID**: {st.session_state.user_id}")
        st.write(f"**会话ID**: {st.session_state.session_id or '无'}")
        st.write(f"**存储方式**: {'Redis' if st.session_state.use_redis else '内存'}")
        st.write(f"**模型**: {model}")
        

    # 主界面
    st.title("Conversational AI")

    # 初始化 Agent 和 Session（如果还没有）
    if st.session_state.agent is None or st.session_state.session is None:
        try:
            agent, session = create_agent_and_session(
                st.session_state.user_id,
                st.session_state.session_id,
                st.session_state.use_redis,
                model
            )
            st.session_state.agent = agent
            st.session_state.session = session
        except Exception as e:
            st.error(f"初始化失败: {str(e)}")
            st.stop()
    
    # 显示聊天历史
    display_chat_history()
    
    # 聊天输入和图片上传移动到底部并分栏
    image_path = None
    image_bytes = None
    prompt = None
    with st._bottom:
        if st.session_state.show_image_upload:
            left_col, right_col = st.columns(2)
            with left_col:
                st.subheader("对话输入")
                prompt = st.chat_input("Type here your question...")
            with right_col:
                uploaded_image = st.file_uploader("上传图片（可选，支持jpg/png）", type=["jpg", "jpeg", "png"])
                if uploaded_image is not None:
                    image_bytes = uploaded_image.read()
                    # 保存到临时文件
                    suffix = "." + uploaded_image.type.split('/')[-1]
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                        tmp_file.write(image_bytes)
                        image_path = tmp_file.name
                    # st.image(image_bytes, caption=None, width=50)
        else:
            prompt = st.chat_input("Type here your question...")

    # 聊天输入逻辑调整为使用 prompt
    if prompt:
        # 显示用户消息
        with st.chat_message("user"):
            # 新增：对用户输入内容做图片宽度限制
            st.markdown(render_markdown_with_img_limit(prompt), unsafe_allow_html=True)
            if image_bytes:
                st.image(image_bytes, caption="本次消息附带图片", width=200)
        
        # 添加用户消息到历史
        user_message = {
            "role": "user",
            "content": prompt,
            "timestamp": time.time()
        }
        if image_path:
            user_message["image_path"] = image_path
        st.session_state.messages.append(user_message)
        
        # 生成助手回复
        with st.chat_message("assistant"):
            with st.spinner("正在思考..."):
                try:
                    # 使用 Agent 生成异步回复，传递 image_source=本地路径
                    reply = asyncio.run(
                        st.session_state.agent.chat(
                            prompt, 
                            st.session_state.session,
                            image_source=image_path if image_path else None
                        )
                    )
                    
                    # 判断是否为 base64 图片 markdown
                    if reply.startswith("![generated image](data:image/png;base64,"):
                        # 提取 base64 数据
                        prefix = "![generated image]("
                        suffix = ")"
                        img_url = reply[len(prefix):-len(suffix)]
                        # 用 HTML 控制最大宽度
                        st.markdown(
                            f'<img src="{img_url}" style="max-width:400px;">',
                            unsafe_allow_html=True
                        )
                    else:
                        # 新增：对助手回复内容做图片宽度限制
                        st.markdown(render_markdown_with_img_limit(reply), unsafe_allow_html=True)
                    
                    # 添加助手消息到历史
                    assistant_message = {
                        "role": "assistant",
                        "content": reply,
                        "timestamp": time.time()
                    }
                    st.session_state.messages.append(assistant_message)
                    
                except Exception as e:
                    error_msg = f"生成回复时出错: {str(e)}"
                    st.error(error_msg)
                    
                    # 添加错误消息到历史
                    error_message = {
                        "role": "assistant",
                        "content": error_msg,
                        "timestamp": time.time()
                    }
                    st.session_state.messages.append(error_message)

if __name__ == "__main__":
    main()
