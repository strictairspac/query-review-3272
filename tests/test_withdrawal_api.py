"""撤下功能的 HTTP 端到端测试（FastAPI TestClient + SQLite + fakeredis）。

覆盖：登录鉴权、viewer 只读 403、可撤下查询、登记/去重、证据详情与下载均
剔除到期行、失败原因与重试，以及“混有未到期账单的旧档 / 发布撞上撤下 / 重启恢复”。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.config.database import get_db
from src.services import withdrawal as wd
from tests.conftest import make_evidence

DETAIL = "SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' ORDER BY invoice_id"
DUE, DUE2, NOT_DUE = 5, 10, 1


@pytest.fixture
def client(session, users, fake_redis):
    # 让 API 的 get_db 依赖返回测试会话，复用同一连接与数据
    app.dependency_overrides[get_db] = lambda: (yield session)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(client, role):
    r = client.post('/auth/login', json={'email': role + '@chenggang.test',
                                         'password': 'ColdChain!17'})
    assert r.status_code == 200
    return r.json()['token']


def _auth(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def detail_batch(session, samples):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'http-batch')
    return bid


def test_viewer_cannot_register_but_can_read(client, detail_batch):
    vt = _token(client, 'viewer')
    # 只读：登记被 403 拒绝
    r = client.post('/withdrawals', json={'batch_id': detail_batch, 'invoice_ids': [DUE]},
                    headers=_auth(vt))
    assert r.status_code == 403
    # 但可以查询台账与可撤下账单
    assert client.get('/withdrawals', headers=_auth(vt)).status_code == 200
    elig = client.get(f'/withdrawals/eligible/{detail_batch}', headers=_auth(vt)).json()
    assert DUE in elig['eligible_invoice_ids']


def test_operator_full_flow_and_export_excludes_due(client, detail_batch):
    ot = _token(client, 'operator')
    # 登记
    r = client.post('/withdrawals', json={'batch_id': detail_batch, 'invoice_ids': [DUE]},
                    headers=_auth(ot))
    assert r.status_code == 202 and r.json()['created_count'] == 1
    wid = r.json()['created'][0]['id']

    # 待处理可查
    pend = client.get('/withdrawals?status=pending', headers=_auth(ot)).json()
    assert any(x['id'] == wid for x in pend)

    # 重复登记不新增
    r2 = client.post('/withdrawals', json={'batch_id': detail_batch, 'invoice_ids': [DUE]},
                     headers=_auth(ot))
    assert r2.json()['created_count'] == 0 and r2.json()['deduplicated_count'] == 1

    # worker 生效
    wd.process_one(wid, db=_db(client))

    # 已生效可查
    eff = client.get('/withdrawals?status=effective', headers=_auth(ot)).json()
    assert any(x['id'] == wid and x['status'] == 'effective' for x in eff)

    # 证据详情与下载都不再带回到期行，未到期行保留
    detail = client.get(f'/evidence/{detail_batch}', headers=_auth(ot)).json()['result']
    assert DUE not in [int(x[0]) for x in detail['original']['rows']]
    assert NOT_DUE in [int(x[0]) for x in detail['original']['rows']]
    export = client.get(f'/evidence/{detail_batch}/export', headers=_auth(ot))
    assert export.status_code == 200
    body = export.content.decode()
    assert f'"{DUE}"' not in body.split('withdrawn_invoice_ids')[0]  # 结果区无到期账单
    assert str(DUE) in body  # 撤下清单里仍记录账单号


def _db(client):
    # 取回被 override 的测试会话
    gen = app.dependency_overrides[get_db]()
    return next(gen)


def test_register_rejects_undue_bill(client, detail_batch):
    at = _token(client, 'admin')
    r = client.post('/withdrawals', json={'batch_id': detail_batch, 'invoice_ids': [NOT_DUE]},
                    headers=_auth(at))
    assert r.status_code == 422 and '尚未到期' in r.json()['detail']


def test_failure_reason_visible_and_retry_reuses_registration(client, detail_batch, monkeypatch):
    at = _token(client, 'admin')
    r = client.post('/withdrawals', json={'batch_id': detail_batch, 'invoice_ids': [DUE]},
                    headers=_auth(at))
    wid = r.json()['created'][0]['id']
    # 制造失败
    monkeypatch.setattr(wd, 'load_due_dates', lambda db: {DUE: '2026-12-31'})
    wd.process_one(wid, db=_db(client))
    failed = client.get('/withdrawals?status=failed', headers=_auth(at)).json()
    assert failed[0]['id'] == wid and '未到期' in failed[0]['fail_reason']
    # 重试沿用同一登记
    monkeypatch.undo()
    import fakeredis
    monkeypatch.setattr(wd, 'queue', fakeredis.FakeRedis(decode_responses=True))
    rr = client.post(f'/withdrawals/{wid}/retry', headers=_auth(at))
    assert rr.status_code == 202 and rr.json()['id'] == wid
    wd.process_one(wid, db=_db(client))
    assert client.get('/withdrawals?status=effective', headers=_auth(at)).json()[0]['id'] == wid


def test_concurrent_publish_colliding_with_withdrawal(client, session, samples, monkeypatch):
    """实测场景：撤下登记后，并发发布的复核候选侧不再带该账单 -> 撤下落 failed。"""
    at = _token(client, 'admin')
    bid, result = make_evidence(session, samples['detail'], DETAIL, 'concurrent-batch')
    r = client.post('/withdrawals', json={'batch_id': bid, 'invoice_ids': [DUE]},
                    headers=_auth(at))
    wid = r.json()['created'][0]['id']
    # 并发发布：候选侧移除该账单（撞车）
    result['candidate']['rows'] = [x for x in result['candidate']['rows'] if int(x[0]) != DUE]
    result['candidate']['row_count'] = len(result['candidate']['rows'])
    from sqlalchemy import select
    from src.models.evidence import Evidence
    ev = session.scalar(select(Evidence).where(Evidence.batch_id == bid))
    ev.result = {**result}
    session.commit()
    wd.process_one(wid, db=session)
    failed = client.get('/withdrawals?status=failed', headers=_auth(at)).json()
    assert failed[0]['id'] == wid and '两侧' in failed[0]['fail_reason']


def test_restart_recovers_pending(client, detail_batch, monkeypatch):
    """实测场景：登记后队列丢失/重启，pending 登记被恢复并重新入队、最终生效。"""
    import fakeredis
    at = _token(client, 'admin')
    r = client.post('/withdrawals', json={'batch_id': detail_batch, 'invoice_ids': [DUE2]},
                    headers=_auth(at))
    wid = r.json()['created'][0]['id']
    # 清空队列模拟重启；恢复逻辑把 pending 重新入队
    wd.queue.flushall()
    recovered = wd.recover_pending()
    assert wid in recovered
    wd.process_one(wid, db=_db(client))
    eff = client.get('/withdrawals?status=effective', headers=_auth(at)).json()
    assert any(x['id'] == wid for x in eff)
