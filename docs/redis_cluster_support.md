# Redis Cluster Support

This document describes the Redis cluster support functionality added to xAgent.

## Overview

The xAgent framework now supports both standalone Redis instances and Redis Cluster deployments. This enhancement allows for better scalability and high availability in production environments.

## Supported Redis Configurations

### Standalone Redis
```
redis://localhost:6379
redis://user:password@localhost:6379
rediss://localhost:6380  # SSL/TLS
```

### Redis Cluster

#### Using Cluster-specific Schemes
```
redis+cluster://localhost:6379
rediss+cluster://localhost:6380  # SSL/TLS cluster
```

#### Using Query Parameters
```
redis://localhost:6379?cluster=true
redis://localhost:6379?cluster=1
redis://localhost:6379?cluster=yes
```

## Components with Cluster Support

The following components now support Redis Cluster:

1. **Message Storage** (`xagent.components.message.redis_messages.MessageStorageRedis`)
2. **Memory Messages** (`xagent.components.memory.utils.messages_for_memory.RedisMessagesForMemory`)

## Implementation Details

### Cluster Detection

The system automatically detects cluster mode using two methods:

1. **URL Scheme Detection**: URLs with `redis+cluster://` or `rediss+cluster://` schemes
2. **Query Parameter Detection**: URLs with `cluster=true|1|yes` query parameters

### Client Creation

The enhanced client creation logic:

```python
def create_redis_client(redis_url: str, **common_kwargs):
    """Create Redis client supporting both standalone and cluster modes."""
    if _looks_like_cluster(redis_url):
        return RedisCluster.from_url(_strip_query_param(redis_url, "cluster"), **common_kwargs)
    else:
        return redis.Redis.from_url(redis_url, **common_kwargs)
```

### Key Features

- **Automatic Detection**: No code changes required for existing applications
- **Backward Compatibility**: Existing Redis URLs continue to work unchanged
- **Error Handling**: Graceful fallback and comprehensive error reporting
- **Type Safety**: Full type annotations for both Redis and RedisCluster clients

## Configuration Examples

### Environment Variables

```bash
# Standalone Redis
export REDIS_URL="redis://localhost:6379"

# Redis Cluster (method 1)
export REDIS_URL="redis+cluster://localhost:6379"

# Redis Cluster (method 2) 
export REDIS_URL="redis://localhost:6379?cluster=true"

# Redis Cluster with authentication
export REDIS_URL="redis+cluster://user:pass@localhost:6379"
```

### Python Code

```python
from xagent.components.message.redis_messages import MessageStorageRedis
from xagent.components.memory.utils.messages_for_memory import RedisMessagesForMemory

# Automatic detection based on URL
message_storage = MessageStorageRedis("redis+cluster://localhost:6379")
memory_messages = RedisMessagesForMemory("redis://localhost:6379?cluster=true")

# Works with existing URLs too
legacy_storage = MessageStorageRedis("redis://localhost:6379")
```

## Testing

A comprehensive test suite is included to verify cluster functionality:

```bash
python test_redis_cluster_support.py
```

The test suite covers:
- Cluster URL detection logic
- Query parameter stripping
- Client creation logic
- Error handling scenarios

## Migration Guide

### From Standalone Redis to Cluster

1. **Update Connection URL**:
   ```bash
   # Old
   REDIS_URL="redis://localhost:6379"
   
   # New
   REDIS_URL="redis+cluster://localhost:6379"
   ```

2. **No Code Changes Required**: The application will automatically detect and use cluster mode.

3. **Verify Configuration**: Use the test script to verify cluster detection:
   ```python
   from xagent.components.message.redis_messages import _looks_like_cluster
   
   # Should return True for cluster URLs
   print(_looks_like_cluster("redis+cluster://localhost:6379"))
   ```

## Performance Considerations

- **Connection Pooling**: Both standalone and cluster clients use optimized connection pooling
- **Health Checks**: Automatic health checks ensure connection reliability
- **Retry Logic**: Built-in retry mechanisms for transient failures
- **Memory Management**: Automatic cleanup and resource management

## Troubleshooting

### Common Issues

1. **Cluster Detection Not Working**:
   - Verify URL format: `redis+cluster://` or `?cluster=true`
   - Check for typos in cluster parameter values

2. **Connection Failures**:
   - Ensure Redis cluster is properly configured
   - Verify network connectivity to cluster nodes
   - Check authentication credentials

3. **Performance Issues**:
   - Monitor connection pool usage
   - Adjust timeout settings if needed
   - Consider connection limits

### Debug Mode

Enable debug logging to troubleshoot connection issues:

```python
import logging
logging.getLogger("MessageStorageRedis").setLevel(logging.DEBUG)
logging.getLogger("RedisMessagesForMemory").setLevel(logging.DEBUG)
```

## References

- [Redis Cluster Documentation](https://redis.io/topics/cluster-tutorial)
- [redis-py Cluster Support](https://redis-py.readthedocs.io/en/stable/cluster_tutorial.html)
- [xAgent Memory Documentation](docs/memory.md)
