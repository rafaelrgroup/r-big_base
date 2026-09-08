"""Adaptador LOCAL de desenvolvimento. Não é o banco definitivo nem importador de produção."""
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

class Store:
    def __init__(self, path):
        self.path = str(path); Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS objects (kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS idempotency (actor TEXT,key TEXT,hash TEXT,response TEXT,PRIMARY KEY(actor,key));
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Append only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Append only'); END;
            ''')
    @contextmanager
    def transaction(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute('PRAGMA foreign_keys=ON'); c.execute('BEGIN IMMEDIATE')
        try: yield c; c.commit()
        except Exception: c.rollback(); raise
        finally: c.close()
    def get(self,c,kind,id):
        r=c.execute('SELECT body FROM objects WHERE kind=? AND id=?',(kind,id)).fetchone()
        return json.loads(r[0]) if r else None
    def all(self,c,kind): return [json.loads(r[0]) for r in c.execute('SELECT body FROM objects WHERE kind=? ORDER BY id',(kind,))]
    def put(self,c,kind,obj): c.execute('INSERT INTO objects VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',(kind,obj['id'],json.dumps(obj,ensure_ascii=False)))
    def event(self,c,obj): c.execute('INSERT INTO events(body) VALUES (?)',(json.dumps(obj,ensure_ascii=False),))
