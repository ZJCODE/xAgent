import sys
import os
import time
from typing import Optional, Union, List
import streamlit as st
import asyncio
import re
import tempfile
import uuid
import httpx
import json

# 页面配置
st.set_page_config(
    page_title="对话测试",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

class AgentHTTPClient:
    """HTTP客户端用于与xAgent服务通信"""
    
    def __init__(self, base_url: str = "http://localhost:8010"):
        self.base_url = base_url.rstrip('/')
        
    async def chat(self, user_message: str, user_id: str, session_id: str, image_source: Optional[Union[str, List[str]]] = None, enable_memory: bool = False):
        """发送聊天消息到Agent服务"""
        try:
            payload = {
                "user_id": user_id,
                "session_id": session_id,
                "user_message": user_message,
                "enable_memory": enable_memory
            }
            if image_source:
                payload["image_source"] = image_source
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat",
                    json=payload
                )
            
            if response.status_code == 200:
                result = response.json()
                return result.get("reply", "")
            else:
                raise Exception(f"HTTP {response.status_code}: {response.text}")
                
        except httpx.RequestError as e:
            raise Exception(f"网络请求失败: {str(e)}")
    
    async def chat_stream(self, user_message: str, user_id: str, session_id: str, image_source: Optional[Union[str, List[str]]] = None, enable_memory: bool = False):
        """发送聊天消息到Agent服务（流式输出）"""
        try:
            payload = {
                "user_id": user_id,
                "session_id": session_id,
                "user_message": user_message,
                "stream": True,
                "enable_memory": enable_memory
            }
            if image_source:
                payload["image_source"] = image_source
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat",
                    json=payload,
                    headers={"Accept": "text/event-stream"}
                ) as response:
                    
                    if response.status_code != 200:
                        raise Exception(f"HTTP {response.status_code}: {await response.aread()}")
                    
                    buffer = ""
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            buffer += chunk.decode('utf-8')
                            lines = buffer.split('\n')
                            buffer = lines[-1]  # 保留最后一行可能不完整的数据
                            
                            for line in lines[:-1]:
                                line = line.strip()
                                if line.startswith('data: '):
                                    data_str = line[6:]  # 移除 'data: ' 前缀
                                    if data_str == '[DONE]':
                                        return
                                    try:
                                        data = json.loads(data_str)
                                        if 'delta' in data:
                                            yield data['delta']
                                        elif 'message' in data:
                                            yield data['message']
                                        elif 'error' in data:
                                            raise Exception(data['error'])
                                    except json.JSONDecodeError:
                                        continue
                                        
        except httpx.RequestError as e:
            raise Exception(f"网络请求失败: {str(e)}")
    
    async def clear_session(self, user_id: str, session_id: str):
        """清空会话历史"""
        try:
            payload = {
                "user_id": user_id,
                "session_id": session_id
            }
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self.base_url}/clear_session",
                    json=payload
                )
            
            if response.status_code == 200:
                return True
            else:
                raise Exception(f"HTTP {response.status_code}: {response.text}")
                
        except httpx.RequestError as e:
            raise Exception(f"网络请求失败: {str(e)}")
    
    def health_check(self):
        """检查服务健康状态"""
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/health")
                return response.status_code == 200
        except:
            return False

# 初始化 Session State
def init_session_state():
    """初始化 Streamlit session state"""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    if "http_client" not in st.session_state:
        st.session_state.http_client = None
    
    if "user_id" not in st.session_state:
        st.session_state.user_id = "streamlit_user"
    
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    
    if "agent_server_url" not in st.session_state:
        # 从环境变量读取初始值，如果没有则使用默认值
        st.session_state.agent_server_url = os.getenv("XAGENT_SERVER_URL", "http://localhost:8010")
    
    if "show_image_upload" not in st.session_state:
        st.session_state.show_image_upload = False
    
    if "enable_streaming" not in st.session_state:
        st.session_state.enable_streaming = True
    
    if "enable_memory" not in st.session_state:
        st.session_state.enable_memory = False

def create_http_client(agent_server_url: str):
    """创建 HTTP 客户端实例"""
    return AgentHTTPClient(base_url=agent_server_url)

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
            
            # 检查是否为字典/JSON对象
            if isinstance(content, dict):
                # 如果是JSON对象，使用st.json展示
                st.json(content)
            elif isinstance(content, str):
                # 判断是否为 base64 图片 markdown
                if content.startswith("![generated image](data:image/png;base64,"):
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
            else:
                # 其他类型，转换为字符串显示
                st.text(str(content))
            
            # 显示多张历史图片（如果有）
            if "image_paths" in message and message["image_paths"]:
                st.caption("附带的图片:")
                # 使用columns来并排显示多张图片
                cols = st.columns(min(len(message["image_paths"]), 3))  # 最多3列
                for i, img_path in enumerate(message["image_paths"]):
                    try:
                        with open(img_path, 'rb') as f:
                            img_bytes = f.read()
                        col_idx = i % 3
                        with cols[col_idx]:
                            st.image(img_bytes, caption=f"图片 {i+1}", width=150)
                    except Exception as e:
                        st.error(f"无法加载图片 {i+1}: {str(e)}")
            
            # 兼容旧版本的单张图片显示
            elif "image_path" in message and message["image_path"]:
                try:
                    with open(message["image_path"], 'rb') as f:
                        img_bytes = f.read()
                    st.image(img_bytes, caption="附带图片", width=200)
                except Exception as e:
                    st.error(f"无法加载图片: {str(e)}")
                
            if "timestamp" in message:
                st.caption(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(message['timestamp']))}")

def main():
    """主函数"""
    init_session_state()
    
    # 侧边栏配置
    with st.sidebar:
        st.title("对话配置")
        
        # 服务器配置
        st.subheader("Agent 服务器设置")
        agent_server_url = st.text_input("Agent 服务器地址", value=st.session_state.agent_server_url)
        
        # 检查服务器连接状态
        if st.session_state.http_client:
            if st.session_state.http_client.health_check():
                st.success("✅ 服务器连接正常")
            else:
                st.error("❌ 服务器连接失败")
        
        # 用户配置
        st.subheader("用户设置")
        user_id = st.text_input("用户ID", value=st.session_state.user_id)
        session_id = st.text_input("会话ID (可选)", value=st.session_state.session_id or "")
        
        # 新增：图片上传模块显示控制
        st.subheader("界面设置")
        show_image_upload = st.checkbox("显示图片上传模块", value=st.session_state.show_image_upload)
        if show_image_upload != st.session_state.show_image_upload:
            st.session_state.show_image_upload = show_image_upload
            st.rerun()
        
        # 新增：流式输出控制
        enable_streaming = st.checkbox("启用流式输出", value=st.session_state.enable_streaming)
        if enable_streaming != st.session_state.enable_streaming:
            st.session_state.enable_streaming = enable_streaming
            st.rerun()
        
        # 新增：记忆功能控制
        enable_memory = st.checkbox("启用记忆功能", value=st.session_state.enable_memory)
        if enable_memory != st.session_state.enable_memory:
            st.session_state.enable_memory = enable_memory
            st.rerun()
        
        # 应用配置按钮
        if st.button("应用配置", type="primary"):
            st.session_state.user_id = user_id
            st.session_state.session_id = session_id if session_id else None
            st.session_state.agent_server_url = agent_server_url
            
            # 重新创建 HTTP 客户端
            try:
                http_client = create_http_client(agent_server_url)
                st.session_state.http_client = http_client
                st.success("配置已应用！")
            except Exception as e:
                st.error(f"配置失败: {str(e)}")
        
        # 清空历史按钮
        if st.button("清空对话历史", type="secondary"):
            if st.session_state.http_client:
                try:
                    # 调用清空会话的 HTTP 接口
                    success = asyncio.run(st.session_state.http_client.clear_session(
                        st.session_state.user_id, 
                        st.session_state.session_id
                    ))
                    if success:
                        st.session_state.messages = []
                        st.success("对话历史已清空！")
                        st.rerun()
                except Exception as e:
                    st.error(f"清空历史失败: {str(e)}")
        
        
    # 主界面
    st.title("Conversational AI")

    # 初始化 HTTP 客户端（如果还没有）
    if st.session_state.http_client is None:
        try:
            http_client = create_http_client(st.session_state.agent_server_url)
            st.session_state.http_client = http_client
        except Exception as e:
            st.error(f"初始化 HTTP 客户端失败: {str(e)}")
            st.stop()
    
    # 显示聊天历史
    display_chat_history()
    
    # 聊天输入和图片上传移动到底部
    image_paths = []
    image_bytes_list = []
    prompt = None
    
    with st._bottom:
        if st.session_state.show_image_upload:
            left_col, right_col = st.columns(2)
            with left_col:
                st.subheader("对话输入")
                prompt = st.chat_input("Type here your question...")
            with right_col:
                uploaded_images = st.file_uploader("上传图片（可选，支持jpg/png，可多选）", type=["jpg", "jpeg", "png"], accept_multiple_files=True)
                if uploaded_images is not None and len(uploaded_images) > 0:
                    for uploaded_image in uploaded_images:
                        image_bytes = uploaded_image.read()
                        image_bytes_list.append(image_bytes)
                        # 保存到临时文件
                        suffix = "." + uploaded_image.type.split('/')[-1]
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                            tmp_file.write(image_bytes)
                            image_paths.append(tmp_file.name)
        else:
            prompt = st.chat_input("Type here your question...")

    # 聊天输入逻辑调整为使用 prompt
    if prompt:
        # 显示用户消息
        with st.chat_message("user"):
            # 新增：对用户输入内容做图片宽度限制
            st.markdown(render_markdown_with_img_limit(prompt), unsafe_allow_html=True)
            if image_bytes_list:
                for i, image_bytes in enumerate(image_bytes_list):
                    st.image(image_bytes, caption=f"图片 {i+1}", width=200)
        
        # 添加用户消息到历史
        user_message = {
            "role": "user",
            "content": prompt,
            "timestamp": time.time()
        }
        if image_paths:
            user_message["image_paths"] = image_paths
        st.session_state.messages.append(user_message)
        
        # 生成助手回复
        with st.chat_message("assistant"):
            try:
                if st.session_state.enable_streaming:
                    # 流式输出模式
                    reply_placeholder = st.empty()
                    full_reply = ""  # 初始化为字符串，但可能会被字典覆盖
                    first_chunk_received = False
                    
                    # 初始显示"正在思考..."
                    with reply_placeholder:
                        st.caption("正在思考...")
                    
                    async def stream_response():
                        nonlocal full_reply, first_chunk_received
                        async for chunk in st.session_state.http_client.chat_stream(
                            user_message=prompt,
                            user_id=st.session_state.user_id,
                            session_id=st.session_state.session_id,
                            image_source=image_paths if image_paths else None,
                            enable_memory=st.session_state.enable_memory
                        ):
                            # 收到第一个chunk时，清除"正在思考..."提示
                            if not first_chunk_received:
                                first_chunk_received = True
                                reply_placeholder.empty()
                            
                            # 检查chunk类型，如果是字典则转换为字符串
                            if isinstance(chunk, dict):
                                # 如果是字典，可能是JSON响应，直接显示并结束
                                reply_placeholder.json(chunk)
                                full_reply = chunk  # 保存为字典
                                return
                            elif isinstance(chunk, str):
                                full_reply += chunk
                                # 实时更新显示
                                if full_reply.startswith("![generated image](data:image/png;base64,"):
                                    # 如果是图片，等待完整后再显示
                                    if full_reply.endswith(")"):
                                        prefix = "![generated image]("
                                        suffix = ")"
                                        img_url = full_reply[len(prefix):-len(suffix)]
                                        reply_placeholder.markdown(
                                            f'<img src="{img_url}" style="max-width:400px;">',
                                            unsafe_allow_html=True
                                        )
                                else:
                                    reply_placeholder.markdown(render_markdown_with_img_limit(full_reply), unsafe_allow_html=True)
                            else:
                                # 其他类型转换为字符串处理
                                chunk_str = str(chunk)
                                full_reply += chunk_str
                                reply_placeholder.markdown(render_markdown_with_img_limit(full_reply), unsafe_allow_html=True)
                    
                    asyncio.run(stream_response())
                    
                    reply = full_reply
                else:
                    # 非流式输出模式
                    with st.spinner("正在思考..."):
                        reply = asyncio.run(
                            st.session_state.http_client.chat(
                                user_message=prompt,
                                user_id=st.session_state.user_id,
                                session_id=st.session_state.session_id,
                                image_source=image_paths if image_paths else None,
                                enable_memory=st.session_state.enable_memory
                            )
                        )
                        
                        # 检查是否为字典/JSON对象
                        if isinstance(reply, dict):
                            # 如果是JSON对象，使用st.json展示
                            st.json(reply)
                        elif isinstance(reply, str):
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
                        else:
                            # 其他类型，转换为字符串显示
                            st.text(str(reply))
                
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
