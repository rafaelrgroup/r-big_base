"""Single-process control plane; never stores person/company records or data jobs."""
import fcntl
import os
import sqlite3
from pathlib import Path

from .store import Store

CONTROL_KINDS = frozenset({'user', 'session', 'challenge', 'api_key', 'key_rotation_receipt',
                           'invitation', 'source', 'field', 'field_definition_version'})


class ControlStore(Store):
    def __init__(self, root):
        root = Path(root)
        if (not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents))
                or root.stat().st_mode & 0o077):
            raise ValueError('PRIVATE_CONTROL_DIRECTORY_REQUIRED')
        for name in ('control.sqlite3', 'encryption.key'):
            path = root/name
            if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError('EXISTING_PRIVATE_CONTROL_STATE_REQUIRED')
        fd = os.open(root/'runtime.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ValueError('CONTROL_RUNTIME_ALREADY_RUNNING') from None
        self._lock_fd = fd
        try:
            path = root/'control.sqlite3'
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as c:
                kinds = {r[0] for r in c.execute('SELECT DISTINCT kind FROM objects')}
                if kinds - CONTROL_KINDS:
                    raise ValueError('PERSON_DATA_OR_JOBS_FORBIDDEN_IN_CONTROL_STORE')
                if not c.execute("SELECT 1 FROM objects WHERE kind='user' LIMIT 1").fetchone():
                    raise ValueError('EXISTING_ADMINISTRATIVE_ACCOUNTS_REQUIRED')
            super().__init__(path)
            with self.transaction() as c:
                allowed = ','.join("'"+kind+"'" for kind in sorted(CONTROL_KINDS))
                for action in ('INSERT', 'UPDATE'):
                    c.execute(f'''CREATE TRIGGER IF NOT EXISTS control_only_{action.lower()}
                        BEFORE {action} ON objects WHEN NEW.kind NOT IN ({allowed})
                        BEGIN SELECT RAISE(ABORT,'Control data only'); END''')
        except Exception:
            self.close()
            raise

    def put(self, c, kind, value):
        if kind not in CONTROL_KINDS:
            raise ValueError('PERSON_DATA_OR_JOBS_FORBIDDEN_IN_CONTROL_STORE')
        return super().put(c, kind, value)

    def close(self):
        if getattr(self, '_lock_fd', None) is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
