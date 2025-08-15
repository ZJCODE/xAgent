# xAgent 一键启动指南

## 简介

新的 `xagent` 命令提供了一键启动 xAgent 服务器和 Web 界面的功能，简化了部署和使用流程。

## 基本用法

### 默认启动（推荐）
```bash
# 同时启动服务器和 Web 界面，使用默认配置
xagent
```

这将启动：
- HTTP 服务器：`http://localhost:8010`
- Web 界面：`http://0.0.0.0:8501`

### 仅启动服务器
```bash
# 只启动 HTTP 服务器，不启动 Web 界面
xagent --server-only
```

### 仅启动 Web 界面
```bash
# 只启动 Web 界面（假设服务器已经在运行）
xagent --web-only
```

## 高级配置

### 自定义配置文件
```bash
# 使用自定义配置文件
xagent --config config/agent.yaml

# 指定工具包路径
xagent --toolkit-path ./my-tools
```

### 自定义端口
```bash
# 自定义服务器和 Web 界面端口
xagent --server-port 8020 --web-port 8502
```

### 自定义主机地址
```bash
# 自定义主机地址
xagent --server-host 0.0.0.0 --web-host localhost
```

### 组合配置
```bash
# 完整的自定义配置示例
xagent \
  --config config/production.yaml \
  --toolkit-path ./enterprise-tools \
  --server-host 0.0.0.0 \
  --server-port 8080 \
  --web-host 127.0.0.1 \
  --web-port 8080
```

## 命令行帮助

查看所有可用选项：
```bash
xagent --help
```

## 停止服务

使用 `Ctrl+C` 停止所有正在运行的服务。系统会优雅地关闭所有进程。

## 环境变量

确保设置了必要的环境变量：
```bash
export OPENAI_API_KEY=your_openai_api_key
export REDIS_URL=your_redis_url  # 可选，用于持久化
```

## 故障排除

### 端口冲突
如果默认端口已被占用，使用自定义端口：
```bash
xagent --server-port 8011 --web-port 8502
```

### 配置文件错误
检查配置文件路径和格式：
```bash
# 使用默认配置
xagent --server-only

# 然后逐步添加自定义配置
```

### 服务无法启动
检查日志输出和错误信息，确保：
1. OpenAI API Key 已正确设置
2. 端口未被其他进程占用
3. 配置文件格式正确

## 与传统命令的对比

| 功能 | 新命令 | 传统命令 |
|------|--------|----------|
| 启动服务器 | `xagent --server-only` | `xagent-server` |
| 启动 Web | `xagent --web-only` | `xagent-web` |
| 启动 CLI | 不支持 | `xagent-cli` |
| 一键启动 | `xagent` | 需要多个终端 |

## 生产环境部署

```bash
# 生产环境推荐配置
xagent \
  --config config/production.yaml \
  --server-host 0.0.0.0 \
  --server-port 8010 \
  --web-host 0.0.0.0 \
  --web-port 8501
```
