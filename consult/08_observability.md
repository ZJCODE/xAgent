# 可观测性与监控

## 1. 当前可观测性状态

| 能力 | 当前状态 | 说明 |
|------|----------|------|
| LLM 调用追踪 | ✅ Langfuse | 通过 `@observe()` 装饰器 |
| 结构化日志 | ⚠️ 基础 | 使用 `logging` 模块但格式不统一 |
| 指标收集 | ❌ 缺失 | 无 Prometheus/StatsD 集成 |
| 分布式追踪 | ❌ 缺失 | 无 OpenTelemetry 集成 |
| 健康检查 | ⚠️ 基础 | 仅返回静态字符串 |
| 错误报告 | ⚠️ 基础 | 只记录到日志 |
| 性能监控 | ❌ 缺失 | 无响应时间追踪 |

---

## 2. 日志系统改进

### 2.1 当前问题

**全局 basicConfig 配置**（`agent.py` 第 24 行）会影响所有使用该模块的应用：

```python
# 当前（污染全局 logging 配置）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
```

这在库代码中是反模式。库应该将日志处理的责任交给应用层，只添加 `NullHandler`。

**改进方案**：

```python
# 库代码中（agent.py）
import logging

# 按照 Python 最佳实践，库只添加 NullHandler
logging.getLogger("xagent").addHandler(logging.NullHandler())

# 应用层（用户的代码）自行配置 logging
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
```

### 2.2 结构化日志

为便于日志收集和分析，推荐使用结构化 JSON 日志：

```python
import structlog
import json

def configure_logging(level: str = "INFO", json_output: bool = False):
    """Configure structured logging for xAgent."""
    
    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

# Agent 中使用结构化日志
class Agent:
    def __init__(self, ...):
        self.logger = structlog.get_logger(
            self.__class__.__name__
        ).bind(agent_name=self.name, model=self.model)
    
    async def chat(self, user_message: str, user_id: str, ...):
        self.logger.info(
            "chat_started",
            user_id=user_id,
            session_id=session_id,
            message_length=len(user_message)
        )
        
        start_time = time.monotonic()
        try:
            result = await self._do_chat(...)
            self.logger.info(
                "chat_completed",
                user_id=user_id,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                result_type=type(result).__name__
            )
            return result
        except Exception as e:
            self.logger.error(
                "chat_failed",
                user_id=user_id,
                error_type=type(e).__name__,
                error_message=str(e)[:200],
                exc_info=True
            )
            raise
```

### 2.3 请求 ID 追踪

为每个请求添加唯一 ID，便于跨日志追踪：

```python
import contextvars

# 使用 contextvars 存储请求 ID
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="unknown"
)

class RequestIDMiddleware:
    """Add unique request ID to each request."""
    
    async def __call__(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request_id_var.set(request_id)
        
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

# 在日志中自动包含请求 ID
class RequestAwareLogger:
    def info(self, msg, **kwargs):
        kwargs["request_id"] = request_id_var.get("unknown")
        structlog.get_logger().info(msg, **kwargs)
```

---

## 3. 指标收集

### 3.1 推荐指标体系

参考 USE（Utilization, Saturation, Errors）和 RED（Rate, Errors, Duration）方法论：

```python
from prometheus_client import Counter, Histogram, Gauge, start_http_server
import time

class AgentMetrics:
    """Prometheus metrics for xAgent monitoring."""
    
    # RED Metrics
    chat_requests_total = Counter(
        "xagent_chat_requests_total",
        "Total number of chat requests",
        ["agent_name", "status"]  # status: success, error
    )
    
    chat_duration_seconds = Histogram(
        "xagent_chat_duration_seconds",
        "Time spent processing chat requests",
        ["agent_name"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
    )
    
    tool_calls_total = Counter(
        "xagent_tool_calls_total",
        "Total number of tool calls",
        ["agent_name", "tool_name", "status"]
    )
    
    tool_duration_seconds = Histogram(
        "xagent_tool_duration_seconds",
        "Time spent executing tool calls",
        ["tool_name"],
        buckets=[0.01, 0.1, 0.5, 1.0, 5.0, 30.0]
    )
    
    # USE Metrics
    active_sessions = Gauge(
        "xagent_active_sessions",
        "Number of currently active sessions",
        ["agent_name"]
    )
    
    memory_retrievals_total = Counter(
        "xagent_memory_retrievals_total",
        "Total memory retrieval operations",
        ["user_id", "status"]
    )
    
    llm_tokens_used_total = Counter(
        "xagent_llm_tokens_used_total",
        "Total LLM tokens consumed",
        ["agent_name", "model", "token_type"]  # token_type: input, output
    )

# 在 Agent 中使用
class InstrumentedAgent(Agent):
    def __init__(self, *args, metrics: Optional[AgentMetrics] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.metrics = metrics or AgentMetrics()
    
    async def chat(self, user_message: str, user_id: str, ...) -> ...:
        start_time = time.monotonic()
        self.metrics.active_sessions.labels(agent_name=self.name).inc()
        
        try:
            result = await super().chat(user_message, user_id, ...)
            self.metrics.chat_requests_total.labels(
                agent_name=self.name, status="success"
            ).inc()
            return result
        
        except Exception as e:
            self.metrics.chat_requests_total.labels(
                agent_name=self.name, status="error"
            ).inc()
            raise
        
        finally:
            duration = time.monotonic() - start_time
            self.metrics.chat_duration_seconds.labels(agent_name=self.name).observe(duration)
            self.metrics.active_sessions.labels(agent_name=self.name).dec()
```

---

## 4. 分布式追踪（OpenTelemetry）

### 4.1 当前状态

xAgent 依赖 Langfuse 进行 LLM 追踪，但缺少：
- 请求从 HTTP 层到 Agent 层的完整追踪链路
- 跨服务的分布式追踪（当使用多个 HTTP Agent 时）

### 4.2 推荐：OpenTelemetry 集成

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

def setup_tracing(service_name: str = "xagent", otlp_endpoint: str = None):
    """Configure OpenTelemetry tracing."""
    
    provider = TracerProvider(
        resource=Resource.create({
            SERVICE_NAME: service_name,
            SERVICE_VERSION: __version__,
        })
    )
    
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)

tracer = trace.get_tracer("xagent")

class Agent:
    async def chat(self, user_message: str, user_id: str, ...) -> ...:
        with tracer.start_as_current_span(
            "agent.chat",
            attributes={
                "agent.name": self.name,
                "agent.model": self.model,
                "user.id": user_id,
                "session.id": session_id,
                "message.length": len(user_message),
            }
        ) as span:
            try:
                result = await self._do_chat(...)
                span.set_attribute("result.type", type(result).__name__)
                return result
            except Exception as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                raise
    
    async def _act(self, tool_call, ...) -> Optional[list]:
        tool_name = getattr(tool_call, "name", "unknown")
        with tracer.start_as_current_span(
            "tool.execute",
            attributes={
                "tool.name": tool_name,
                "agent.name": self.name
            }
        ) as span:
            ...
```

---

## 5. 健康检查增强

### 5.1 深度健康检查

当前健康检查只返回静态响应，无法反映实际系统状态：

```python
# 当前（无意义）
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "xAgent HTTP Server"}  # 始终返回 healthy
```

**改进方案**：

```python
from enum import Enum
from typing import Dict

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

class ComponentHealth(BaseModel):
    status: HealthStatus
    latency_ms: Optional[float] = None
    message: Optional[str] = None

class HealthResponse(BaseModel):
    status: HealthStatus
    version: str
    timestamp: str
    components: Dict[str, ComponentHealth]

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Deep health check verifying all critical components."""
    components = {}
    overall_status = HealthStatus.HEALTHY
    
    # Check OpenAI connectivity
    try:
        start = time.monotonic()
        # 使用轻量级 API 调用测试连通性
        await asyncio.wait_for(
            self.agent.client.models.retrieve("gpt-4o-mini"),
            timeout=5.0
        )
        components["openai"] = ComponentHealth(
            status=HealthStatus.HEALTHY,
            latency_ms=int((time.monotonic() - start) * 1000)
        )
    except asyncio.TimeoutError:
        components["openai"] = ComponentHealth(
            status=HealthStatus.DEGRADED,
            message="OpenAI API response slow (>5s)"
        )
        overall_status = HealthStatus.DEGRADED
    except Exception as e:
        components["openai"] = ComponentHealth(
            status=HealthStatus.UNHEALTHY,
            message=f"OpenAI API unreachable: {type(e).__name__}"
        )
        overall_status = HealthStatus.UNHEALTHY
    
    # Check message storage
    try:
        start = time.monotonic()
        await asyncio.wait_for(
            self.message_storage.get_messages("health_check", "test", 1),
            timeout=2.0
        )
        components["message_storage"] = ComponentHealth(
            status=HealthStatus.HEALTHY,
            latency_ms=int((time.monotonic() - start) * 1000)
        )
    except Exception as e:
        components["message_storage"] = ComponentHealth(
            status=HealthStatus.UNHEALTHY,
            message=str(e)[:100]
        )
        if overall_status != HealthStatus.UNHEALTHY:
            overall_status = HealthStatus.DEGRADED
    
    return HealthResponse(
        status=overall_status,
        version=__version__,
        timestamp=datetime.utcnow().isoformat(),
        components=components
    )
```

---

## 6. 成本追踪

### 6.1 LLM 成本监控

对于 xAgent 这类 LLM 密集型框架，成本控制至关重要：

```python
class CostTracker:
    """Track and alert on LLM API costs."""
    
    # 截至 2025 年的价格（每 1M tokens）
    PRICING = {
        "gpt-4.1": {"input": 2.00, "output": 8.00},
        "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
        "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
        "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    }
    
    def __init__(self, daily_budget_usd: float = 10.0):
        self.daily_budget = daily_budget_usd
        self._daily_cost: Dict[str, float] = {}  # user_id -> cost
        self._reset_date = datetime.now().date()
    
    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        user_id: str = "system"
    ) -> float:
        """Record token usage and return cost in USD."""
        self._maybe_reset_daily()
        
        pricing = self.PRICING.get(model, {"input": 0.01, "output": 0.03})
        cost = (
            input_tokens * pricing["input"] / 1_000_000 +
            output_tokens * pricing["output"] / 1_000_000
        )
        
        self._daily_cost[user_id] = self._daily_cost.get(user_id, 0) + cost
        
        # Alert if approaching budget
        total_daily = sum(self._daily_cost.values())
        if total_daily > self.daily_budget * 0.8:
            logger.warning(
                "Approaching daily budget: $%.2f / $%.2f (%.0f%%)",
                total_daily, self.daily_budget, 100 * total_daily / self.daily_budget
            )
        
        return cost
    
    def _maybe_reset_daily(self):
        today = datetime.now().date()
        if today > self._reset_date:
            self._daily_cost = {}
            self._reset_date = today
```

---

## 7. 告警规则建议

```yaml
# alerting_rules.yml (Prometheus/Grafana)
groups:
  - name: xagent_alerts
    rules:
    
      # 高错误率告警
      - alert: HighChatErrorRate
        expr: |
          rate(xagent_chat_requests_total{status="error"}[5m]) /
          rate(xagent_chat_requests_total[5m]) > 0.1
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "High chat error rate (>10%)"
      
      # 响应时间告警
      - alert: SlowChatResponse
        expr: |
          histogram_quantile(0.95, xagent_chat_duration_seconds_bucket) > 30
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P95 chat response time >30s"
      
      # LLM 不可用告警
      - alert: LLMUnavailable
        expr: xagent_health_component_status{component="openai"} == 2  # UNHEALTHY
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "OpenAI API is unavailable"
```

---

## 8. 可观测性改进优先级汇总

| 改进项 | 收益 | 难度 | 优先级 |
|--------|------|------|--------|
| 移除全局 basicConfig | 库兼容性 | 低 | P1 |
| 深度健康检查 | 运维可见性 | 低 | P1 |
| 请求 ID 追踪 | 问题排查 | 低 | P1 |
| 结构化日志 | 日志分析 | 中 | P2 |
| Prometheus 指标 | 监控告警 | 中 | P2 |
| 成本追踪 | 成本控制 | 中 | P2 |
| OpenTelemetry 追踪 | 分布式追踪 | 高 | P3 |
| 告警规则 | 运维自动化 | 低 | P3 |
