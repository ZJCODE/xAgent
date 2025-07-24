# iLanguage - AI驱动的智能英语学习平台

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o-orange.svg)](https://openai.com)
[![Redis](https://img.shields.io/badge/Redis-Latest-red.svg)](https://redis.io)
[![Streamlit](https://img.shields.io/badge/Streamlit-Latest-ff6b6b.svg)](https://streamlit.io)

## 🎯 项目简介

iLanguage 是一个功能完整的AI驱动智能英语学习平台，集成了**词汇学习**、**智能对话**、**个性化推荐**等核心功能。项目采用现代化微服务架构，提供RESTful API服务和交互式Web界面，为用户提供沉浸式的英语学习体验。

### ✨ 核心特色
- 🤖 **AI智能助手**：基于OpenAI GPT-4o的智能对话系统
- 📚 **智能词汇学习**：AI驱动的词汇查询、解释和管理
- 🎨 **多模态交互**：支持文本对话、图像生成、网络搜索
- 📊 **个性化推荐**：基于学习行为的智能词汇推荐算法
- 💾 **数据持久化**：Redis存储，支持用户学习记录追踪
- 🌐 **双界面支持**：RESTful API + Streamlit Web界面

## 🏗️ 系统架构

### 核心模块

#### 🧠 AI对话系统
- **Session管理**：支持多用户、多会话的消息历史管理
- **Agent框架**：异步AI代理，支持工具调用和函数执行
- **工具集成**：词汇查询、网络搜索、图像生成等内置工具
- **存储灵活性**：支持内存存储和Redis持久化两种模式

#### 📖 智能词汇系统
- **AI词汇解释**：基于GPT-4o的智能词汇定义和例句生成
- **难度分级**：自动识别词汇难度（初级/中级/高级/专家）
- **熟悉度追踪**：0-10级熟悉度评分和学习进度追踪
- **智能推荐**：基于遗忘曲线和多维度因素的复习推荐

#### 🎨 多模态功能
- **图像生成**：集成DALL-E图像生成功能
- **网络搜索**：实时信息检索和内容获取
- **数学计算**：内置计算工具支持

### 技术栈

#### 后端核心
- **FastAPI** - 高性能异步Web框架
- **Uvicorn** - ASGI服务器
- **Pydantic** - 数据验证和序列化
- **AsyncIO** - 异步编程支持

#### AI服务
- **OpenAI GPT-4o/GPT-4o-mini** - 大语言模型
- **DALL-E** - 图像生成模型
- **Langfuse** - AI应用监控和追踪
- **Tenacity** - 重试机制和错误处理

#### 数据存储
- **Redis** - 高性能缓存和数据持久化
- **Python-dotenv** - 环境变量管理

#### 前端界面
- **Streamlit** - 交互式Web应用框架
- **现代化UI** - 响应式设计和用户体验优化

#### 开发工具
- **Pytest** - 完整的单元测试覆盖
- **分层架构** - 清晰的代码组织和模块化设计

## 📁 项目结构

```
iLanguage/
├── 🏗️ 核心业务层
│   ├── core/
│   │   ├── agent.py          # AI对话代理核心类
│   │   ├── vocabulary.py     # 词汇服务核心类
│   │   └── base.py          # 基础服务类
│   └── tools/               # 工具函数集
│       ├── vocabulary_tool.py # 词汇查询工具
│       └── openai_tool.py    # OpenAI集成工具
├── 🌐 API服务层
│   ├── api/
│   │   ├── vocabulary.py     # 词汇相关接口
│   │   └── health.py        # 健康检查接口
│   └── main.py              # FastAPI应用入口
├── 💾 数据访问层
│   ├── db/
│   │   ├── vocabulary_db.py  # 词汇数据库操作
│   │   └── message_db.py    # 消息数据库操作
│   └── schemas/             # 数据模型定义
│       ├── vocabulary.py     # 词汇数据模型
│       └── messages.py      # 消息数据模型
├── 🖥️ 前端界面
│   └── frontend/
│       └── chat_app.py      # Streamlit对话界面
├── 🧪 测试模块
│   └── test/
│       ├── test_vocabulary_db.py
│       ├── test_message_db.py
│       └── test_redis_basic.py
├── 🔧 工具和配置
│   ├── utils/
│   │   └── tool_decorator.py # 工具装饰器
│   ├── requirements.txt     # 项目依赖
│   ├── .env                 # 环境变量配置
│   └── README.md           # 项目文档
```

## 快速开始

### 1. 环境准备

确保你的系统已安装：
- Python 3.12+
- Redis 服务器
- OpenAI API 访问权限

### 2. 安装依赖

```bash
# 克隆项目
git clone <repository-url>
cd iLanguage

# 安装依赖
pip install -r requirements.txt
```

### 3. 环境变量配置

在项目根目录创建 `.env` 文件：

```env
# OpenAI API 配置
OPENAI_API_KEY=your_openai_api_key_here

# Redis 配置
REDIS_URL=redis://localhost:6379/0

# Langfuse 配置（可选）
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key
LANGFUSE_SECRET_KEY=your_langfuse_secret_key
LANGFUSE_HOST=https://cloud.langfuse.com
```

### 4. 启动服务

```bash
# 方式1：直接运行
python main.py

# 方式2：使用 uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. 访问服务

- **API 文档**：http://localhost:8000/docs
- **健康检查**：http://localhost:8000/health
- **词汇查询示例**：POST http://localhost:8000/lookup

## API 接口文档

### 词汇查询接口

#### POST /lookup
查询单词详细信息，支持缓存和个性化存储。

**请求参数：**
```json
{
  "word": "sophisticated",
  "user_id": "user123",
  "cache": true
}
```

**响应示例：**
```json
{
  "word": "sophisticated",
  "explanation": "Having great knowledge or experience; complex and refined",
  "example_sentences": [
    "She has a sophisticated understanding of art.",
    "The software uses sophisticated algorithms."
  ],
  "difficulty_level": "advanced",
  "user_id": "user123",
  "familiarity": 0,
  "create_timestamp": 1642694400.0,
  "update_timestamp": 1642694400.0
}
```

#### POST /get_vocabulary
获取用户需要复习的词汇列表。

**请求参数：**
```json
{
  "user_id": "user123",
  "n": 10,
  "exclude_known": false
}
```

### 健康检查接口

#### GET /health
检查服务运行状态。

## 数据模型

### 词汇难度等级
- `BEGINNER` - 初级（A1-A2）
- `INTERMEDIATE` - 中级（B1-B2）
- `ADVANCED` - 高级（C1-C2）
- `EXPERT` - 专家级

### 词汇记录字段
- `word` - 词汇（小写）
- `explanation` - 词汇解释
- `example_sentences` - 例句列表
- `difficulty_level` - 难度等级
- `familiarity` - 熟悉度（0-10）
- `create_timestamp` - 创建时间
- `update_timestamp` - 更新时间
- `last_reviewed_timestamp` - 最后复习时间


## 运行测试

```bash
# 运行所有测试
pytest

# 运行特定测试文件
pytest test/test_vocabulary_db.py

# 运行测试并显示覆盖率
pytest --cov=core --cov=db --cov=api

# 详细测试输出
pytest -v
```

## 开发指南

### 代码结构
- **分层架构**：清晰的业务逻辑分离
- **依赖注入**：便于测试和扩展
- **错误处理**：完善的异常处理机制
- **日志监控**：集成 Langfuse 追踪

### 扩展建议
1. 在 `core/` 目录添加新的业务逻辑
2. 在 `api/` 目录添加新的路由接口
3. 在 `schemas/` 目录定义数据模型
4. 在 `test/` 目录添加对应测试

### 环境变量
```bash
# 开发环境推荐设置
export PYTHONPATH=$(pwd)
export ENVIRONMENT=development
```

## 依赖说明

| 依赖包 | 版本要求 | 用途 |
|--------|----------|------|
| fastapi | latest | Web 框架 |
| uvicorn | latest | ASGI 服务器 |
| pydantic | latest | 数据验证 |
| redis | latest | 数据存储 |
| openai | latest | AI 服务 |
| langfuse | latest | AI 监控 |
| tenacity | latest | 重试机制 |
| python-dotenv | latest | 环境变量 |
| pytest | latest | 测试框架 |

## 许可证

本项目采用 MIT 许可证。详见 LICENSE 文件。

## 贡献指南

1. Fork 本项目
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

## 联系方式

如有问题或建议，请通过以下方式联系：
- 提交 Issue
- 发送邮件
- 参与讨论