"""本地受限环境测试夹具：用 SQLite 替代 Postgres、fakeredis 替代 Redis。

生产仍运行 Postgres/Redis（见 docker-compose 与 requirements）。这里在进程内
把 engine 指向一个文件型 SQLite、队列指向 fakeredis，并按 seed_demo 同一套
演示数据口径建表，用于端到端验证撤下登记业务。表结构只建一次，每个测试清表重灌。
"""
import os
import sys
import tempfile
import contextvars
from pathlib import Path

import fakeredis

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DATABASE_URL', 'sqlite://')  # 占位，下面直接替换 engine
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.config import database as db_cfg
from src.config.database import Base
# 显式导入全部 ORM 模型，确保 Base.metadata 在建表前登记完整（含 audit）。
from src.models import user, sample, batch, evidence, withdrawal, audit  # noqa: F401
from src.models.user import User
from src.models.sample import Sample
from src.models.batch import Batch
from src.models.evidence import Evidence
from src.models.withdrawal import Withdrawal
from src.utils.password import hash_password

from sqlalchemy.pool import StaticPool
# 内存库 + StaticPool：所有连接复用同一个底层 sqlite3 连接，天然共享数据，
# 不存在多连接事务快照互不可见的问题，契合单进程测试。
engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
from sqlalchemy import event


@event.listens_for(engine, 'connect')
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA foreign_keys=ON')
    cur.close()


TestSession = sessionmaker(engine, expire_on_commit=False)
db_cfg.engine = engine
db_cfg.Session = TestSession

# ContextVar 挂到稳定的后端模块上，避免 conftest 被重复导入时出现多份变量
import src.services.query_engine as _qemod
_qemod._test_session_var = contextvars.ContextVar('qr_test_session', default=None)
_current_session = _qemod._test_session_var

# query_engine 导入时绑定 engine，这里指向同一库，并用 SQLite 可执行的 SELECT
# 版本替代 Postgres 专有的 EXPLAIN ANALYZE。
from src.services import query_engine as _qe
_qe.engine = engine


def _sqlite_execute(sql):
    sql = _qe.validate_sql(sql)
    import time
    started = time.perf_counter()
    # 复用当前测试会话的连接，保证读到夹具写入的数据（StaticPool 单连接约束）
    sess = _qemod._test_session_var.get()
    if sess is not None:
        cursor = sess.execute(text(sql))
        columns = list(cursor.keys())
        rows = cursor.fetchmany(1001)
    else:
        with engine.connect() as conn:
            cursor = conn.execute(text(sql))
            columns = list(cursor.keys())
            rows = cursor.fetchmany(1001)
    if len(rows) > 1000:
        raise ValueError('结果超过 1000 行')
    rows = [[str(v) if v is not None else None for v in row] for row in rows]
    return {'columns': columns, 'rows': rows, 'row_count': len(rows),
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 3),
            'plan': [{'Plan': {'Node Type': 'SQLite-test-stand-in'}}]}


_qe.execute_query = _sqlite_execute


def _ensure_schema():
    # 延迟到首次使用：此时 src.main 等已导入全部模型，metadata 完整
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS invoices (invoice_id INTEGER PRIMARY KEY, customer TEXT, route TEXT, "
            "amount NUMERIC, temperature NUMERIC, status TEXT, due_date TEXT)"))

DETAIL_SQL = "SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' ORDER BY invoice_id"
DIFF_SQL = "SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' AND invoice_id <= 40 ORDER BY invoice_id"
ORIGINAL_AGG = "SELECT customer, COUNT(*) AS c FROM invoices GROUP BY customer ORDER BY customer"


def _reseed_invoices(conn):
    conn.execute(text("DELETE FROM invoices"))
    rows = []
    for i in range(1, 601):
        customer = ['澄港海产', '南港鲜食', '北湾冷运'][i % 3]
        route = ['东线', '西线'][i % 2]
        amount = 35 + (i % 17) * 7
        temp = 2 + (i % 9)
        # status 与到期解耦：i%7==0 为 pending；到期日单独控制。
        # i%5==0 的账单到期（含 settled，使明细结果里混有到期行），其余未到期。
        status = 'pending' if i % 7 == 0 else 'settled'
        due = '2026-10-31' if i % 5 == 0 else '2026-12-31'
        rows.append({'a': i, 'b': customer, 'c': route, 'd': amount,
                     'e': temp, 'f': status, 'g': due})
    conn.execute(text(
        "INSERT INTO invoices VALUES (:a,:b,:c,:d,:e,:f,:g)"), rows)


@pytest.fixture
def session():
    _ensure_schema()
    s = TestSession()
    _qemod._test_session_var.set(s)
    for tbl in reversed(Base.metadata.sorted_tables):
        s.execute(tbl.delete())
    s.execute(text("DELETE FROM invoices"))
    rows = []
    for i in range(1, 601):
        customer = ['澄港海产', '南港鲜食', '北湾冷运'][i % 3]
        route = ['东线', '西线'][i % 2]
        amount = 35 + (i % 17) * 7
        temp = 2 + (i % 9)
        # status 与到期解耦：i%7==0 为 pending；到期日单独控制。
        # i%5==0 的账单到期（含 settled，使明细结果里混有到期行），其余未到期。
        status = 'pending' if i % 7 == 0 else 'settled'
        due = '2026-10-31' if i % 5 == 0 else '2026-12-31'
        rows.append({'a': i, 'b': customer, 'c': route, 'd': amount,
                     'e': temp, 'f': status, 'g': due})
    s.execute(text(
        "INSERT INTO invoices VALUES (:a,:b,:c,:d,:e,:f,:g)"), rows)
    s.commit()
    try:
        yield s
    finally:
        _qemod._test_session_var.set(None)
        s.close()


@pytest.fixture
def users(session):
    for role in ('admin', 'operator', 'viewer'):
        session.add(User(email=role + '@chenggang.test', role=role,
                         password_hash=hash_password('ColdChain!17')))
    session.commit()
    return {role: role + '@chenggang.test' for role in ('admin', 'operator', 'viewer')}


@pytest.fixture
def samples(session):
    out = {}
    for key, name, sql in [
        ('detail', '逐单结算明细', DETAIL_SQL),
        ('agg', '客户聚合', ORIGINAL_AGG),
        ('diff', '差异明细', DIFF_SQL),
    ]:
        s = Sample(name=name, description='d', original_sql=sql)
        session.add(s)
        session.commit()
        out[key] = s.id
    return out


def make_evidence(session, sample_id, candidate_sql, name, equivalent=True):
    """绕过真实 SQL 执行，直接构造一份带 invoice_id 的证据，模拟 worker 留档。"""
    from src.services.query_engine import execute_query
    sample = session.get(Sample, sample_id)
    left = execute_query(sample.original_sql)
    right = execute_query(candidate_sql)
    if not equivalent:
        # 候选少一些行，制造 missing/extra 差异样本
        right['rows'] = [row for row in right['rows'] if int(row[0]) > 14]
        right['row_count'] = len(right['rows'])
    import json
    from collections import Counter
    import hashlib
    canon = lambda d: Counter(json.dumps(r, ensure_ascii=False) for r in d['rows'])
    a, b = canon(left), canon(right)
    digest = lambda x: hashlib.sha256(
        json.dumps(sorted(canon(x).items()), ensure_ascii=False).encode()).hexdigest()
    result = {
        'equivalent': equivalent,
        'comparison': 'test',
        'original': left, 'candidate': right,
        'original_checksum': digest(left), 'candidate_checksum': digest(right),
        'missing_rows': list((a - b).elements())[:10],
        'extra_rows': list((b - a).elements())[:10],
        'note': 'test',
    }
    batch = Batch(name=name, sample_id=sample_id, candidate_sql=candidate_sql,
                  status='different' if not equivalent else 'passed')
    session.add(batch)
    session.commit()
    session.add(Evidence(batch_id=batch.id, result=result))
    session.commit()
    return batch.id, result


@pytest.fixture
def fake_redis(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    import src.services.batch as bsvc
    import src.services.withdrawal as wsvc
    monkeypatch.setattr(bsvc, 'queue', r)
    monkeypatch.setattr(wsvc, 'queue', r)
    return r
