import redis
import pytest
import os
from dotenv import load_dotenv

load_dotenv(override=True)

@pytest.fixture(scope="module")
def redis_client():
    url = os.environ["REDIS_URL"]
    client = redis.Redis.from_url(url)
    yield client
    client.close()

def test_set_and_get(redis_client):
    redis_client.set('foo', 'bar')
    value = redis_client.get('foo')
    assert value == b'bar'
