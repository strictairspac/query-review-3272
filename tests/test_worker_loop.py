"""worker 队列分发与重启恢复测试：走真实 worker 模块的 drain/tick 逻辑。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest
import fakeredis

from src.services import withdrawal as wd
from src import worker
from src.constants.enums import WITHDRAW_QUEUE
from src.models.withdrawal import Withdrawal
from tests.conftest import make_evidence

DETAIL = "SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' ORDER BY invoice_id"
DUE = 5


@pytest.fixture
def patched_worker(monkeypatch, fake_redis, session):
    # worker 在模块导入时绑定了 queue，这里换成 fakeredis
    monkeypatch.setattr(worker, 'queue', fake_redis)
    monkeypatch.setattr(worker.wd, 'queue', fake_redis)

    # StaticPool 单连接约束下，让 worker 自建的 Session 复用测试会话（close 置空，
    # 避免 worker 关闭共享连接）。worker 与 withdrawal 各持一份 Session 引用。
    class _Shared:
        def __getattr__(self, name):
            if name == 'close':
                return lambda: None
            return getattr(session, name)

        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    factory = lambda: _Shared()
    monkeypatch.setattr(worker, 'Session', factory)
    monkeypatch.setattr(wd, 'Session', factory)
    return fake_redis


def test_worker_drains_withdrawal_queue(session, samples, patched_worker):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'worker-batch')
    wid = wd.create_withdrawals(session, bid, [DUE])['created'][0]['id']
    # 队列里有一条撤下任务
    assert patched_redis_has(patched_worker, WITHDRAW_QUEUE, wid)
    # worker 一轮 tick 消费并生效
    assert worker.tick() is True
    assert session.get(Withdrawal, wid).status == 'effective'
    # 队列已清空
    assert patched_worker.llen(WITHDRAW_QUEUE) == 0


def test_worker_recovers_after_restart(session, samples, patched_worker):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'restart-batch')
    wid = wd.create_withdrawals(session, bid, [DUE])['created'][0]['id']
    # 模拟重启：队列被清空，但 DB 里登记仍是 pending
    patched_worker.flushall()
    assert session.get(Withdrawal, wid).status == 'pending'
    # 空轮询 tick 触发 recover_pending，把 pending 重新入队，随后下一轮处理
    assert worker.tick() is False  # 第一轮：恢复入队
    assert patched_redis_has(patched_worker, WITHDRAW_QUEUE, wid)
    assert worker.tick() is True   # 第二轮：消费并生效
    assert session.get(Withdrawal, wid).status == 'effective'


def test_retry_then_worker_effective_without_new_registration(session, samples, patched_worker):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'retry-worker-batch')
    created = wd.create_withdrawals(session, bid, [DUE])
    wid = created['created'][0]['id']
    # 重试不新增登记，并重新入队
    wd.retry_withdrawal(session, wid)
    assert session.query(Withdrawal).count() == 1
    worker.tick()
    assert session.get(Withdrawal, wid).status == 'effective'


def patched_redis_has(r, key, wid):
    return str(wid) in r.lrange(key, 0, -1)
