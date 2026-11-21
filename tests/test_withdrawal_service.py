"""撤下登记服务的端到端业务测试（SQLite + fakeredis，不依赖 Postgres/Redis 服务）。

演示口径：i%5==0 的账单到期(2026-10-31)，其中非 7 倍数的为 settled，
因此“逐单结算明细”（settled）结果里天然混有到期账单，如 5、10、15、20。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest
from fastapi import HTTPException

from src.models.withdrawal import Withdrawal
from src.services import withdrawal as wd
from src.services import evidence as ev_svc
from tests.conftest import make_evidence

DETAIL = "SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' ORDER BY invoice_id"
DUE, DUE2, NOT_DUE = 5, 10, 1


@pytest.fixture
def detail_batch(session, samples):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'detail-batch')
    return bid


def test_eligible_only_lists_due_bilateral(session, detail_batch, fake_redis):
    info = wd.eligible_for_batch(session, detail_batch)
    # 明细两侧都带账单号；到期的 settled 账单 5/10 可撤下
    assert DUE in info['eligible_invoice_ids'] and DUE2 in info['eligible_invoice_ids']
    assert NOT_DUE not in info['eligible_invoice_ids']
    assert NOT_DUE in info['bilateral_invoice_ids']


def test_register_rejects_not_due(session, detail_batch, fake_redis):
    with pytest.raises(HTTPException) as exc:
        wd.create_withdrawals(session, detail_batch, [NOT_DUE])
    assert exc.value.status_code == 422 and '尚未到期' in exc.value.detail


def test_register_rejects_non_bilateral(session, samples, fake_redis):
    # 聚合证据没有 invoice_id 列，任何账单都不能登记
    bid, _ = make_evidence(session, samples['agg'],
                          "SELECT customer, COUNT(*) AS c FROM invoices GROUP BY customer ORDER BY customer",
                          'agg-batch')
    with pytest.raises(HTTPException) as exc:
        wd.create_withdrawals(session, bid, [DUE])
    assert exc.value.status_code == 422 and '两侧' in exc.value.detail


def test_full_lifecycle_with_due_bill(session, samples, fake_redis):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'due-batch')
    info = wd.eligible_for_batch(session, bid)
    assert DUE in info['eligible_invoice_ids']

    created = wd.create_withdrawals(session, bid, [DUE])
    assert created['created_count'] == 1
    wid = created['created'][0]['id']
    assert created['created'][0]['status'] == 'pending'
    # 已推入撤下队列
    assert fake_redis.lrange('query-review:withdrawals', 0, -1) == [str(wid)]

    # 重复登记不新增（去重、不新增登记）
    again = wd.create_withdrawals(session, bid, [DUE])
    assert again['created_count'] == 0 and again['deduplicated_count'] == 1
    assert session.query(Withdrawal).count() == 1

    # worker 处理 -> effective
    wd.process_one(wid)
    obj = session.get(Withdrawal, wid)
    assert obj.status == 'effective' and obj.fail_reason is None

    # 证据视图：到期行在页面/下载中被剔除，未到期行保留，原行数留档
    view = ev_svc.get_evidence(session, bid)['result']
    assert DUE not in [int(r[0]) for r in view['original']['rows']]
    assert DUE not in [int(r[0]) for r in view['candidate']['rows']]
    assert NOT_DUE in [int(r[0]) for r in view['original']['rows']]
    assert view['original_row_count_original'] > view['original']['row_count']
    assert DUE in view['withdrawn_invoice_ids']
    # 原始留档本身未被改写，旧档仍可查
    raw = ev_svc.get_raw_evidence(session, bid)['result']
    assert DUE in [int(r[0]) for r in raw['original']['rows']]


def test_retry_does_not_create_new_registration(session, samples, fake_redis, monkeypatch):
    import fakeredis
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'retry-batch')
    wid = wd.create_withdrawals(session, bid, [DUE])['created'][0]['id']

    # 人为制造失败：让账单变成未到期
    monkeypatch.setattr(wd, 'load_due_dates', lambda db: {DUE: '2026-12-31'})
    wd.process_one(wid, db=session)
    failed = session.get(Withdrawal, wid)
    assert failed.status == 'failed' and '未到期' in failed.fail_reason

    # 恢复到期口径后重试，沿用同一条登记，不新增
    monkeypatch.undo()
    # undo 会连带撤销 fake_redis 的 patch，这里重新装回，保证重试只入内存队列
    monkeypatch.setattr(wd, 'queue', fakeredis.FakeRedis(decode_responses=True))
    retried = wd.retry_withdrawal(session, wid)
    assert retried['id'] == wid and retried['status'] == 'pending'
    assert session.query(Withdrawal).count() == 1
    wd.process_one(wid, db=session)
    assert session.get(Withdrawal, wid).status == 'effective'


def test_interrupted_pending_is_recovered(session, samples, fake_redis):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'recover-batch')
    wid = wd.create_withdrawals(session, bid, [DUE])['created'][0]['id']
    fake_redis.flushall()  # 模拟队列丢失 / 进程中断

    recovered = wd.recover_pending()
    assert wid in recovered
    assert str(wid) in fake_redis.lrange('query-review:withdrawals', 0, -1)


def test_failed_when_bill_no_longer_bilateral(session, samples, fake_redis):
    bid, result = make_evidence(session, samples['detail'], DETAIL, 'stale-batch')
    wid = wd.create_withdrawals(session, bid, [DUE])['created'][0]['id']
    # 候选侧不再带该账单（并发发布了不含该账单的复核）
    result['candidate']['rows'] = [r for r in result['candidate']['rows'] if int(r[0]) != DUE]
    result['candidate']['row_count'] = len(result['candidate']['rows'])
    from sqlalchemy import select as _sel2
    from src.models.evidence import Evidence as _Ev
    # 整体重新赋值，确保 JSON 变更被持久化
    ev_obj = session.scalar(_sel2(_Ev).where(_Ev.batch_id == bid))
    ev_obj.result = {**result}
    session.commit()
    wd.process_one(wid, db=session)  # 同会话，确保读到上面并发改写后的证据
    obj = session.get(Withdrawal, wid)
    assert obj.status == 'failed' and '两侧' in obj.fail_reason


def test_diff_samples_withdrawn_on_different_evidence(session, samples, fake_redis):
    # 差异复核：到期账单 5 在两侧都带账单号但金额不同 -> 同时进入 missing/extra，
    # 撤下生效后该差异样本同步剔除，而原结论(different)保持不变。
    import json
    from collections import Counter
    bid, result = make_evidence(session, samples['detail'], DETAIL, 'diff-batch',
                                equivalent=True)
    # 把候选侧账单 5 的金额改掉，制造差异
    for row in result['candidate']['rows']:
        if int(row[0]) == DUE:
            row[3] = '999.99'
    a = Counter(json.dumps(r, ensure_ascii=False) for r in result['original']['rows'])
    b = Counter(json.dumps(r, ensure_ascii=False) for r in result['candidate']['rows'])
    result['missing_rows'] = list((a - b).elements())[:10]
    result['extra_rows'] = list((b - a).elements())[:10]
    result['equivalent'] = False
    from sqlalchemy import select as _sel
    from src.models.evidence import Evidence
    ev_obj = session.scalar(_sel(Evidence).where(Evidence.batch_id == bid))
    ev_obj.result = {**result}  # 整体重新赋值，确保 JSON 变更持久化
    session.commit()

    missing_ids = {int(json.loads(s)[0]) for s in result['missing_rows']}
    assert DUE in missing_ids  # 到期账单 5 出现在差异样本里

    wd.create_withdrawals(session, bid, [DUE])
    wd.process_one(wd.list_withdrawals(session, status='pending')[0]['id'], db=session)
    view = ev_svc.get_evidence(session, bid)['result']
    left_ids = {int(json.loads(s)[0]) for s in view['missing_rows']}
    extra_ids = {int(json.loads(s)[0]) for s in view['extra_rows']}
    assert DUE not in left_ids and DUE not in extra_ids
    assert view['equivalent'] is False  # 原结论不变


def test_status_filter_and_listing(session, samples, fake_redis):
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'list-batch')
    wd.create_withdrawals(session, bid, [DUE, DUE2])
    pend = wd.list_withdrawals(session, status='pending')
    assert len(pend) == 2
    wd.process_one(pend[0]['id'])
    assert len(wd.list_withdrawals(session, status='effective')) == 1
    with pytest.raises(HTTPException):
        wd.list_withdrawals(session, status='bogus')


def test_withdrawal_is_global_across_old_and_concurrent_reviews(session, samples, fake_redis):
    """关键口径：账单在批次 A 登记撤下生效后，旧档与并发发布的复核 B（同样含该账单）
    在页面/下载中都不能再带回到期行，即便 B 从未登记过撤下。"""
    # 批次 A（旧档）与批次 B（并发发布的复核），候选 SQL 不同但都含到期账单 DUE
    bid_a, _ = make_evidence(session, samples['detail'], DETAIL, 'old-archive')
    bid_b, _ = make_evidence(session, samples['detail'],
                             DETAIL + ' LIMIT 400', 'concurrent-review')

    # 只在 A 上登记并生效
    wid = wd.create_withdrawals(session, bid_a, [DUE])['created'][0]['id']
    wd.process_one(wid, db=session)
    assert session.get(Withdrawal, wid).status == 'effective'

    # A 剔除到期行
    view_a = ev_svc.get_evidence(session, bid_a)['result']
    assert DUE not in [int(r[0]) for r in view_a['original']['rows']]
    assert DUE in view_a['withdrawn_invoice_ids']

    # B 没有任何撤下登记，但因全局生效同样剔除该到期行
    assert wd.effective_invoice_ids(session, bid_b) == set()  # B 自身无登记
    view_b = ev_svc.get_evidence(session, bid_b)['result']
    assert DUE not in [int(r[0]) for r in view_b['original']['rows']]
    assert DUE in view_b['withdrawn_invoice_ids']
    # B 的未到期行照旧保留，原行数留档
    assert NOT_DUE in [int(r[0]) for r in view_b['original']['rows']]
    assert view_b['original_row_count_original'] > view_b['original']['row_count']

    # 原始留档（未叠加撤下）仍完整可查
    raw_b = ev_svc.get_raw_evidence(session, bid_b)['result']
    assert DUE in [int(r[0]) for r in raw_b['original']['rows']]
