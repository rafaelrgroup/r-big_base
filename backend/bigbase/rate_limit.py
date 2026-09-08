"""Limites compartilhados entre processos, com falha fechada quando Redis falha."""
import hashlib
from redis import Redis
from redis.exceptions import RedisError
from redis.retry import Retry
from redis.backoff import NoBackoff

TOKEN_BUCKET = '''
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + tonumber(clock[2]) / 1000
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local old = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(old[1]) or burst
local previous = tonumber(old[2]) or now
local at = math.max(now, previous)
tokens = math.min(burst, tokens + math.max(0, now - previous) * rate / 1000)
local allowed = 0
local retry = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1
else retry = math.ceil((1 - tokens) * 1000 / rate) end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'at', at)
redis.call('PEXPIRE', KEYS[1], math.ceil(burst * 2000 / rate) + 1000)
return {allowed, retry}
'''

class RateLimitUnavailable(RuntimeError):
    pass

class RedisLimiter:
    def __init__(self, url, namespace='bigbase:limits:v1'):
        self.client = Redis.from_url(url, socket_connect_timeout=.3,
            socket_timeout=.3, max_connections=16,
            retry=Retry(NoBackoff(), 0), decode_responses=True)
        self.script = self.client.register_script(TOKEN_BUCKET)
        self.namespace = namespace

    def consume(self, key, rate=20, burst=40):
        if rate <= 0 or burst < 1:
            raise ValueError('Taxa e capacidade devem ser positivas')
        digest = hashlib.sha256(key.encode()).hexdigest()
        try:
            result = self.script(keys=[self.namespace + ':' + digest], args=[rate, burst])
        except RedisError as exc:
            raise RateLimitUnavailable('Coordenador de limites indisponível') from exc
        return bool(result[0])

    def close(self):
        self.client.close()
