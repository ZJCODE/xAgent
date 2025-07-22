# iLanguage 项目

## 项目简介
iLanguage 是一个基于 Python 的极简 FastAPI 框架项目，旨在提供清晰的分层结构和最佳实践。项目包含核心业务逻辑、API 路由、数据模型校验，并集成 Redis 作为后端存储，便于快速开发和扩展。

## 目录结构
- `core/`         业务核心功能类
- `api/`          FastAPI 路由接口
- `schemas/`      Pydantic 数据模型与校验
- `db/`           数据库相关操作（如 Redis 封装）
- `main.py`       FastAPI 应用入口
- `test/`         单元测试用例

## 快速开始

1. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

2. **环境变量配置**
   在项目根目录下创建 `.env` 文件，配置 Redis 连接等环境变量，例如：
   ```env
   REDIS_URL=redis://localhost:6379/0
   ```

3. **启动服务**
   ```bash
   uvicorn main:app --reload
   ```

4. **访问接口文档**
   打开浏览器访问 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) 查看自动生成的 API 文档。

## 运行测试

- 运行指定测试文件：
  ```bash
  pytest test/test_vocabulary_db.py
  ```
- 运行全部测试：
  ```bash
  pytest
  ```

## 说明

- 可按需扩展 `core/`、`api/`、`schemas/`、`db/` 目录下的功能模块。
- 遵循分层架构，便于维护和扩展。
- 推荐在开发环境下设置环境变量：
  ```bash
  export PYTHONPATH=$(pwd)
  ```

## 依赖环境

- Python 3.12+
- FastAPI
- Uvicorn
- Redis (可选，依赖于具体业务需求)