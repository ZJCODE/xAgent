# 开发者体验优化

## 1. 开发环境配置

### 1.1 缺少开发依赖声明

`pyproject.toml` 没有定义 `dev` 依赖组，新贡献者不知道应该安装哪些开发工具：

```toml
# 建议添加
[project.optional-dependencies]
dev = [
    # 测试
    "pytest>=8.0",
    "pytest-asyncio>=0.25",
    "pytest-cov>=6.0",
    "pytest-httpserver>=1.0",
    
    # 代码质量
    "ruff>=0.9",           # 快速 linter + formatter
    "mypy>=1.15",          # 类型检查
    "pre-commit>=4.0",     # Git hooks
    
    # 文档
    "mkdocs-material>=9.0",
    "mkdocstrings[python]>=0.27",
]
```

开发者只需 `pip install -e ".[dev]"` 即可获得完整开发环境。

### 1.2 缺少 pre-commit 配置

世界顶级开源项目（如 FastAPI、Pydantic）都使用 pre-commit hooks 确保代码质量：

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.15.0
    hooks:
      - id: mypy
        additional_dependencies: [pydantic>=2.0]
  
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-toml
      - id: check-json
      - id: debug-statements
      - id: check-added-large-files
```

### 1.3 缺少 Makefile/任务运行器

顶级项目都提供便捷的开发命令：

```makefile
# Makefile
.PHONY: install test lint format type-check docs clean

install:
	pip install -e ".[dev]"
	pre-commit install

test:
	pytest tests/ -v --cov=xagent --cov-report=term-missing

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-e2e:
	pytest tests/e2e/ -v -m e2e

lint:
	ruff check xagent/ tests/

format:
	ruff format xagent/ tests/

type-check:
	mypy xagent/

docs-serve:
	mkdocs serve

docs-build:
	mkdocs build

clean:
	find . -type d -name __pycache__ -delete
	find . -type f -name "*.pyc" -delete
	rm -rf .coverage htmlcov/ dist/ build/
```

---

## 2. API 易用性改进

### 2.1 入口点过于分散

用户需要从多个路径导入：

```python
# 当前（分散的导入路径）
from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow
from xagent.multi.swarm import Swarm
from xagent.interfaces.server import AgentHTTPServer
from xagent.utils.tool_decorator import function_tool
```

**改进方案**：提供统一的顶层 API：

```python
# 目标：从 xagent 直接导入所有常用类
from xagent import (
    Agent,
    Workflow,
    Swarm,
    AgentHTTPServer,
    function_tool,
    # 内置工具
    web_search,
    python_executor,
    # 记忆组件
    MemoryStorageLocal,
    MemoryStorageUpstash,
)

# xagent/__init__.py
from .core.agent import Agent
from .multi.workflow import Workflow
from .multi.swarm import Swarm
from .interfaces.server import AgentHTTPServer
from .utils.tool_decorator import function_tool
from .components.memory import MemoryStorageLocal, MemoryStorageUpstash
from .__version__ import __version__

__all__ = [
    "Agent",
    "Workflow", 
    "Swarm",
    "AgentHTTPServer",
    "function_tool",
    "MemoryStorageLocal",
    "MemoryStorageUpstash",
    "__version__",
]
```

### 2.2 配置 API 改进

当前 `Agent` 使用大量位置参数，对 IDE 自动补全不友好：

```python
# 当前（参数众多，难以记忆）
agent = Agent(
    name="my_agent",
    system_prompt="You are helpful...",
    description="A helpful assistant",
    model="gpt-4.1-mini",
    client=None,
    tools=[web_search],
    mcp_servers=["http://..."],
    sub_agents=[],
    output_type=None,
    message_storage=None,
    memory_storage=None,
)
```

**改进方案**：提供 Builder 模式或配置对象：

```python
# 方案1：配置对象
class AgentConfig(BaseModel):
    name: str = "assistant"
    model: str = "gpt-4.1-mini"
    system_prompt: Optional[str] = None
    description: Optional[str] = None
    tools: List[Callable] = Field(default_factory=list)
    mcp_servers: List[str] = Field(default_factory=list)
    max_iter: int = 10
    max_concurrent_tools: int = 10
    enable_memory: bool = False
    memory_threshold: int = 10

agent = Agent(config=AgentConfig(
    name="my_agent",
    model="gpt-4.1-mini",
    tools=[web_search]
))

# 方案2：Builder 模式
agent = (
    Agent.builder()
    .name("my_agent")
    .model("gpt-4.1-mini")
    .tools(web_search, python_executor)
    .system_prompt("You are an expert programmer")
    .with_memory(enabled=True, threshold=10)
    .build()
)
```

### 2.3 快速入门助手

提供 `xagent init` 命令生成项目脚手架：

```python
# xagent/cli.py 中添加 init 命令

@click.command()
@click.argument("project_name")
@click.option("--template", type=click.Choice(["basic", "server", "workflow"]), default="basic")
def init_project(project_name: str, template: str):
    """Initialize a new xAgent project."""
    
    templates = {
        "basic": BASIC_TEMPLATE,
        "server": SERVER_TEMPLATE,
        "workflow": WORKFLOW_TEMPLATE
    }
    
    # Create project directory
    os.makedirs(project_name, exist_ok=True)
    
    # Generate files from template
    template_content = templates[template]
    with open(f"{project_name}/agent.py", "w") as f:
        f.write(template_content)
    
    with open(f"{project_name}/.env.example", "w") as f:
        f.write("OPENAI_API_KEY=your_api_key_here\n")
    
    print(f"✅ Project '{project_name}' created successfully!")
    print(f"Next steps:")
    print(f"  1. cd {project_name}")
    print(f"  2. cp .env.example .env")
    print(f"  3. Edit .env and add your API key")
    print(f"  4. python agent.py")
```

---

## 3. 文档改进

### 3.1 API 参考文档

目前项目缺少自动生成的 API 参考文档。建议使用 MkDocs + mkdocstrings：

```yaml
# mkdocs.yml
site_name: xAgent Documentation
site_url: https://zjcode.github.io/xAgent/
theme:
  name: material
  features:
    - navigation.tabs
    - navigation.sections
    - content.code.copy
    - search.suggest

plugins:
  - search
  - mkdocstrings:
      handlers:
        python:
          options:
            show_source: true
            show_docstring_examples: true

nav:
  - Home: index.md
  - Quick Start: quickstart.md
  - Guides:
    - Creating Your First Agent: guides/first-agent.md
    - Working with Tools: guides/tools.md
    - Memory System: guides/memory.md
    - Multi-Agent Workflows: guides/workflows.md
    - HTTP Server: guides/server.md
  - API Reference:
    - Agent: api/agent.md
    - Workflow: api/workflow.md
    - Memory: api/memory.md
    - Tools: api/tools.md
  - Examples: examples/index.md
  - Contributing: contributing.md
  - Changelog: changelog.md
```

### 3.2 CONTRIBUTING.md

缺少贡献指南，会阻碍社区参与：

```markdown
# Contributing to xAgent

We welcome contributions! Here's how to get started.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/ZJCODE/xAgent
cd xAgent

# Install with development dependencies
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
```

## Code Style

We use Ruff for linting and formatting:

```bash
make lint     # Check for issues
make format   # Auto-fix formatting
make type-check  # Type checking with mypy
```

## Running Tests

```bash
make test          # Run all tests
make test-unit     # Fast unit tests only
```

## Submitting Changes

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes with tests
4. Ensure all tests pass: `make test`
5. Submit a pull request

## Pull Request Guidelines

- Write clear commit messages
- Add tests for new features
- Update documentation if needed
- Keep changes focused and minimal
```

### 3.3 Changelog

缺少版本变更日志，影响用户升级决策：

```markdown
# Changelog

All notable changes to xAgent are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `function_tool` decorator for easy tool creation
- Multi-modal image support
- MCP server integration

### Fixed
- Thread safety in tool spec cache

## [0.2.33] - 2025-XX-XX

### Added  
- ...
```

---

## 4. 错误信息友好性

### 4.1 当前错误信息不够友好

工具注册失败时的错误信息不清晰：

```python
# 当前
raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
```

**改进方案**：提供上下文和解决方案：

```python
raise TypeError(
    f"Tool '{fn.tool_spec['name']}' must be an async function.\n"
    f"  Problem: The tool function is synchronous.\n"
    f"  Solution: Add 'async' keyword to your function definition:\n"
    f"    Before: def {fn.__name__}(...):\n"
    f"    After:  async def {fn.__name__}(...):\n"
    f"  Note: If your function uses I/O operations, ensure they are awaitable."
)
```

### 4.2 缺少配置验证

Agent 接受无效配置时不报错，在运行时才失败：

```python
# 建议：在 __init__ 中验证关键配置
class Agent:
    def __init__(self, ...):
        # 立即验证配置，而非等到运行时
        if model and not isinstance(model, str):
            raise TypeError(f"model must be a string, got {type(model).__name__}")
        
        if history_count <= 0:
            raise ValueError(f"history_count must be positive, got {history_count}")
        
        if max_iter <= 0:
            raise ValueError(f"max_iter must be positive, got {max_iter}")
```

---

## 5. 版本管理改进

### 5.1 语义化版本控制

建议严格遵循 [Semantic Versioning](https://semver.org/)，并在 `CHANGELOG.md` 中记录每个版本的变更。

### 5.2 自动发布 CI

```yaml
# .github/workflows/release.yml
name: Release

on:
  push:
    tags:
      - "v*"

jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      id-token: write
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Build package
        run: pip install build && python -m build
      
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
      
      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          body_path: CHANGELOG.md
          files: dist/*
```

---

## 6. 社区建设

### 6.1 Issue 和 PR 模板

```markdown
<!-- .github/ISSUE_TEMPLATE/bug_report.md -->
---
name: Bug Report
about: Report a bug in xAgent
labels: bug
---

## Bug Description
A clear description of what the bug is.

## Minimal Reproducible Example
```python
import asyncio
from xagent import Agent

# Minimal code to reproduce the bug
agent = Agent()

async def main():
    result = await agent.chat("...")
    print(result)

asyncio.run(main())
```

## Expected Behavior
What you expected to happen.

## Actual Behavior
What actually happened (include full error traceback if applicable).

## Environment
- Python version:
- xAgent version:
- OS:
```

### 6.2 Code of Conduct

添加行为准则文件（`CODE_OF_CONDUCT.md`），建立社区规范：

```markdown
# Contributor Covenant Code of Conduct

## Our Pledge

We pledge to make participation in our community a harassment-free 
experience for everyone, regardless of background.

## Our Standards

Examples of positive behavior:
- Using welcoming and inclusive language
- Respecting differing viewpoints and experiences
- Gracefully accepting constructive criticism

## Enforcement

Instances of abusive behavior may be reported to the project maintainers.
All complaints will be reviewed and investigated.
```

---

## 7. 开发者体验优先级汇总

| 改进项 | 受益群体 | 难度 | 优先级 |
|--------|----------|------|--------|
| 顶层 `__init__.py` 导入 | 所有用户 | 低 | P1 |
| dev 依赖声明 | 贡献者 | 低 | P1 |
| CONTRIBUTING.md | 贡献者 | 低 | P1 |
| pre-commit 配置 | 贡献者 | 低 | P2 |
| Makefile 开发命令 | 贡献者 | 低 | P2 |
| 友好的错误信息 | 开发者 | 中 | P2 |
| API 文档（MkDocs） | 所有用户 | 中 | P2 |
| Changelog | 所有用户 | 低 | P2 |
| Builder 模式 API | 高级用户 | 中 | P3 |
| `xagent init` 命令 | 新用户 | 中 | P3 |
| 自动发布 CI | 维护者 | 低 | P3 |
