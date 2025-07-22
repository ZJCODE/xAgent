# iLanguage - AI驱动的英语学习平台

## 项目简介

iLanguage 是一个基于 FastAPI 和 OpenAI GPT-4o 的智能英语学习平台，专注于词汇学习和语言能力提升。项目采用现代化的分层架构设计，集成了 Redis 数据存储、AI 智能查询、词汇管理等功能，为用户提供个性化的英语学习体验。

## 核心功能

### 🎯 智能词汇查询
- **AI驱动解释**：基于 OpenAI GPT-4o-mini 提供准确的词汇定义和解释
- **智能缓存**：Redis 缓存机制，提升查询效率
- **个性化存储**：用户词汇记录持久化存储
- **难度分级**：自动识别词汇难度等级（初级/中级/高级/专家）

### 📚 词汇管理系统
- **熟悉度追踪**：0-10级熟悉度评分系统
- **复习推荐**：基于遗忘曲线的智能复习提醒
- **例句管理**：丰富的例句库，支持添加和管理
- **学习统计**：详细的学习进度和词汇统计

### 🔄 智能推荐算法
- **多路召回**：结合熟悉度、时间间隔等多维度因素
- **分层采样**：优先覆盖不同难度级别的词汇
- **个性化排序**：根据用户学习情况动态调整推荐优先级

## 技术栈

### 后端框架
- **FastAPI** - 现代化的 Python Web 框架
- **Uvicorn** - ASGI 服务器
- **Pydantic** - 数据验证和序列化

### AI 服务
- **OpenAI GPT-4o-mini** - 智能词汇解释和内容生成
- **Langfuse** - AI 应用监控和追踪
- **Tenacity** - 重试机制和错误处理

### 数据存储
- **Redis** - 高性能缓存和数据存储
- **Python-dotenv** - 环境变量管理

### 开发工具
- **Pytest** - 单元测试框架
- **分层架构** - 清晰的代码组织结构

## 项目结构

```
iLanguage/
├── core/                 # 核心业务逻辑层
│   ├── vocabulary.py     # 词汇服务核心类
│   ├── content.py        # 内容生成服务
│   ├── conversation.py   # 对话练习服务
│   └── base.py          # 基础服务类
├── api/                  # API 路由层
│   ├── vocabulary.py     # 词汇相关接口
│   └── health.py        # 健康检查接口
├── schemas/              # 数据模型定义
│   ├── vocabulary.py     # 词汇数据模型
│   └── health.py        # 健康检查模型
├── db/                   # 数据访问层
│   └── vocabulary_db.py  # Redis 数据库操作
├── test/                 # 测试文件
│   ├── test_vocabulary_db.py
│   └── test_redis_basic.py
├── static/               # 静态文件（预留）
├── examples/             # 示例代码
├── main.py              # FastAPI 应用入口
├── requirements.txt     # 项目依赖
└── README.md           # 项目文档
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
  "save": true,
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