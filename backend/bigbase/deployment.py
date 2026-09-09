"""Explicit deployed runtime: existing identity only, no fixture fallback or DDL."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID
import json
import os
import re
import stat
from datetime import datetime, timezone
import math
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict

from .canonical_store import CanonicalStore
from .canonical_http import CanonicalReads


def private_text(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('DEPLOYMENT_PRIVATE_FILE_REQUIRED')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o037 or info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o040 and info.st_gid != os.getgid()) or info.st_size > 65536:
        raise ValueError('DEPLOYMENT_PRIVATE_FILE_REQUIRED')
    return path.read_text().strip()


@dataclass(frozen=True)
class DeploymentIdentity:
    deployment_id: str
    environment: str
    database: str
    schema: str
    port: int

    def __post_init__(self):
        if (str(UUID(self.deployment_id)) != self.deployment_id or self.environment not in {'staging', 'production'}
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,50}', self.database)
                or self.database in {'bigbase_test', 'postgres', 'template0', 'template1'}
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,50}', self.schema)
                or type(self.port) is not int or self.port != 15432):
            raise ValueError('EXPLICIT_DEPLOYMENT_IDENTITY_REQUIRED')

    def validate_dsn(self, dsn):
        values = conninfo_to_dict(dsn)
        if (values.get('host') != '127.0.0.1' or values.get('hostaddr') or values.get('service')
                or values.get('dbname') != self.database or values.get('port') != str(self.port)
                or not values.get('user') or values.get('options')
                or os.environ.get('PGSERVICE') or os.environ.get('PGHOSTADDR') or os.environ.get('PGOPTIONS')):
            raise ValueError('DEPLOYMENT_DSN_MISMATCH')

    def verify(self, info):
        wanted = {'deployment_id': self.deployment_id, 'environment': self.environment,
                  'database': self.database, 'schema': self.schema,
                  'server_port': self.port}
        if (any(info.get(k) != v for k, v in wanted.items())
                or info.get('server_address') not in {'127.0.0.1','127.0.0.1/32'}
                or not 180000 <= info.get('server_version_num', 0) < 190000):
            raise ValueError('DEPLOYMENT_DATABASE_IDENTITY_MISMATCH')


class DeploymentStore(CanonicalStore):
    def __init__(self, dsn, identity):
        self.identity = identity
        identity.validate_dsn(dsn)
        super().__init__(dsn, schema=identity.schema)

    @contextmanager
    def connection(self):
        self.identity.validate_dsn(self.dsn)
        with super().connection() as c:
            # Pin the transaction before identity reads; paginated readers repeat
            # this same level, preserving their existing stable-snapshot contract.
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            self.identity.verify(self._deployment(c))
            # Explicit capability bound to this already-verified transaction;
            # synthetic callers without it retain all original fixture guards.
            c.bigbase_deployment_identity = self.identity
            c.execute("SET LOCAL statement_timeout = '30s'")
            yield c

    def initialize(self, *args, **kwargs):
        raise ValueError('RUNTIME_SCHEMA_INITIALIZATION_FORBIDDEN')


class DeploymentReads(CanonicalReads):
    def __init__(self, repository, *, writes_enabled=False, search_reader=None):
        if not isinstance(repository, DeploymentStore) or type(writes_enabled) is not bool:
            raise ValueError('EXPLICIT_DEPLOYMENT_STORE_REQUIRED')
        self.repository = repository
        self.deployment_id = repository.identity.deployment_id
        self.environment = repository.identity.environment
        self.auth_storage = 'sqlite-control-only-single-process'
        self.writes_enabled = writes_enabled
        self.search_reader = search_reader
        self.search_resource = None
        self.verify()

    def verify(self):
        self.repository.identity.validate_dsn(self.repository.dsn)
        self.repository.identity.verify(self.repository.deployment_info())

    def search_enabled(self):
        return bool(self.search_reader) and (self.search_resource is None or self.search_resource.available())

    def close(self):
        if self.search_resource is not None:self.search_resource.shutdown()


@dataclass(frozen=True)
class DeploymentRuntime:
    identity: DeploymentIdentity
    control_dir: Path
    redis_url: str
    allowed_hosts: tuple
    status_file: Path | None = None

    def __post_init__(self):
        if not self.control_dir.is_absolute() or not self.allowed_hosts or any(
                not isinstance(host, str) or not re.fullmatch(r'[a-z0-9.-]+', host) for host in self.allowed_hosts):
            raise ValueError('DEPLOYMENT_RUNTIME_CONFIGURATION_REQUIRED')
        address = urlsplit(self.redis_url)
        if address.scheme == 'redis':
            if address.hostname != '127.0.0.1':
                raise ValueError('PRIVATE_REDIS_REQUIRED')
        elif address.scheme != 'unix' or not address.path.startswith('/'):
            raise ValueError('PRIVATE_REDIS_REQUIRED')

    def health(self, reads):
        reads.verify()
        migration,complete=load_runtime_status(self.status_file)
        return {'status': 'ok', 'environment': self.identity.environment, 'runtime': 'deployed',
                'production_connected': self.identity.environment == 'production',
                'canonical_connected': True, 'synthetic': False,
                'auth_storage': 'sqlite-control-only-single-process',
                'storage_mode': 'canonical', 'local_person_data_enabled': False, 'writes_enabled': reads.writes_enabled,
                'search_enabled': reads.search_enabled(), 'bulk_enabled': False,
                'migration_complete': complete, 'migration': migration}


def load_configuration(path):
    body = json.loads(private_text(path))
    if set(body)-{'environment', 'canonical', 'control_dir', 'redis_url_file', 'allowed_hosts', 'writes_enabled', 'status_file', 'search_reader_file'} or {'environment','canonical','control_dir','redis_url_file','allowed_hosts','writes_enabled'}-set(body):
        raise ValueError('UNSUPPORTED_DEPLOYMENT_CONFIGURATION')
    canonical = body['canonical']
    if set(canonical) != {'dsn_file', 'deployment_id', 'database', 'schema', 'port'}:
        raise ValueError('UNSUPPORTED_CANONICAL_CONFIGURATION')
    if type(body['writes_enabled']) is not bool or not isinstance(body['allowed_hosts'], list):
        raise ValueError('INVALID_DEPLOYMENT_FLAGS')
    identity = DeploymentIdentity(environment=body['environment'], **{k:v for k,v in canonical.items() if k != 'dsn_file'})
    runtime = DeploymentRuntime(identity, Path(body['control_dir']), private_text(body['redis_url_file']), tuple(body['allowed_hosts']),Path(body['status_file']) if body.get('status_file') else None)
    repository = DeploymentStore(private_text(canonical['dsn_file']), identity)
    reads = DeploymentReads(repository, writes_enabled=body['writes_enabled'])
    if 'search_reader_file' in body:
        if not isinstance(body['search_reader_file'],str) or not body['search_reader_file']:
            raise ValueError('EXPLICIT_SEARCH_READER_FILE_REQUIRED')
        from .deployment_search import load_search
        search = load_search(body['search_reader_file'],identity)
        reads.search_resource = search
        reads.search_reader = search
    return runtime, reads


def load_runtime_status(path, *, now=None):
    unknown={'status':'not_reported','phase':None,'processed':None,'total':None,'progress_percent':None}
    if path is None:return unknown,None
    try:
        value=json.loads(private_text(path))
        if not isinstance(value,dict) or type(value.get('version')) is not int or value['version'] != 1:raise ValueError()
        at=value['updated_at']
        if not isinstance(at,str):raise ValueError()
        stamp=datetime.fromisoformat(at.replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError()
        state=value['migration']
        if not isinstance(state,dict):raise ValueError()
        status=state['status']
        if not isinstance(status,str):raise ValueError()
        if status not in {'restoring','preparing','running','verifying','paused','needs_attention','completed','not_reported'}:raise ValueError()
        phase=state.get('phase')
        if phase is not None and (not isinstance(phase,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',phase)):raise ValueError()
        processed,total=state.get('processed'),state.get('total')
        for count in (processed,total):
            if count is not None and (type(count) is not int or count<0):raise ValueError()
        if processed is not None and total is not None and processed>total:raise ValueError()
        percent=state.get('progress_percent')
        if percent is not None and (type(percent) not in {int,float} or not math.isfinite(percent) or not 0<=percent<=100):raise ValueError()
        verified=state.get('completion_verified',False)
        if type(verified) is not bool:raise ValueError()
        if status=='completed':
            if (not verified or processed is None or total is None or processed!=total
                    or not re.fullmatch(r'[0-9a-f]{64}',state.get('verification_sha256') or '')):raise ValueError()
        elif verified:raise ValueError()
        # Recompute on every health read; file mtime cannot renew a report.
        age=((now or datetime.now(timezone.utc))-stamp).total_seconds()
        freshness='clock_skew' if age < -30 else 'stale' if age > 120 else 'fresh'
        public={'status':status,'phase':phase,'processed':processed,'total':total,
                'progress_percent':percent,'updated_at':at,'freshness':freshness,
                'report_age_seconds':max(0,round(age,3))}
        if freshness != 'fresh':
            public.update(status='needs_attention',phase='needs_attention',
                          reported_status=status,reported_phase=phase)
            return public,None
        return public,status=='completed'
    except (OSError,ValueError,KeyError,TypeError):return unknown,None
