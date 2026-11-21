"""到期账单撤下登记：创建、查询、重试与逐张处理。

设计要点：
- 只有“两侧都带账单号”的复核证据才能登记，且账单必须已到期；
- 同一批次同一账单只有一条登记（唯一约束），重试沿用原登记，不新增；
- 登记后进入 pending 并推入 Redis 队列，worker 逐张处理为 effective/failed；
- 撤下中断可恢复：worker 启动/空轮询时回收 pending 登记重新处理。
"""
from sqlalchemy import select
from fastapi import HTTPException
from ..config.database import Session
from ..config.settings import REDIS_URL
from ..constants.enums import WITHDRAW_QUEUE, WITHDRAW_STATUSES
from ..models.withdrawal import Withdrawal
from ..models.batch import Batch
from ..models.evidence import Evidence
from . import withdrawal_filter as wf

try:  # 生产使用真实 Redis
    from redis import Redis
    queue = Redis.from_url(REDIS_URL)
except Exception:  # 受限本地环境的兜底
    queue = None


def _q():
    return queue


def enqueue(withdrawal_id):
    q = _q()
    if q is not None:
        q.rpush(WITHDRAW_QUEUE, withdrawal_id)


def serialize(x):
    return {
        'id': x.id, 'batch_id': x.batch_id, 'invoice_id': x.invoice_id,
        'status': x.status, 'fail_reason': x.fail_reason,
        'created_at': str(x.created_at), 'updated_at': str(x.updated_at),
    }


def load_due_dates(db):
    """读取账单到期日。invoices 由 seed 建立，缺列/缺表时返回空映射（全部按未到期处理）。"""
    from sqlalchemy import text
    try:
        rows = db.execute(text('SELECT invoice_id, due_date FROM invoices')).all()
    except Exception:
        return {}
    out = {}
    for invoice_id, due in rows:
        if due is not None:
            out[int(invoice_id)] = due
    return out


def _get_evidence_result(db, batch_id):
    ev = db.scalar(select(Evidence).where(Evidence.batch_id == batch_id))
    if not ev:
        raise HTTPException(404, '该批次还没有复核证据，无法登记撤下')
    batch = db.get(Batch, batch_id)
    return batch, ev


def eligible_for_batch(db, batch_id):
    """页面登记面板用：返回该证据可撤下的到期账单与不可撤下原因。"""
    batch, ev = _get_evidence_result(db, batch_id)
    result = ev.result or {}
    if result.get('error'):
        return {'batch_id': batch_id, 'eligible_invoice_ids': [],
                'reason': '该批次执行失败，没有可撤下的结果行'}
    due_dates = load_due_dates(db)
    bilateral = sorted(wf.bilateral_invoice_ids(result))
    eligible = sorted(wf.eligible_invoice_ids(result, due_dates))
    existing = {x.invoice_id for x in db.scalars(
        select(Withdrawal).where(Withdrawal.batch_id == batch_id))}
    return {
        'batch_id': batch_id,
        'batch_name': batch.name if batch else None,
        'bilateral_invoice_ids': bilateral,
        'eligible_invoice_ids': eligible,
        'already_registered': sorted(existing),
        'available_invoice_ids': [i for i in eligible if i not in existing],
    }


def list_withdrawals(db, status=None, batch_id=None):
    stmt = select(Withdrawal).order_by(Withdrawal.id.desc())
    if status:
        if status not in WITHDRAW_STATUSES:
            raise HTTPException(422, '状态仅支持 pending/effective/failed')
        stmt = stmt.where(Withdrawal.status == status)
    if batch_id is not None:
        stmt = stmt.where(Withdrawal.batch_id == batch_id)
    return [serialize(x) for x in db.scalars(stmt)]


def create_withdrawals(db, batch_id, invoice_ids):
    """登记一张或多张到期账单撤下。已存在的登记不重复创建、不新增行。"""
    if not invoice_ids:
        raise HTTPException(422, '请至少选择一张账单')
    if len(invoice_ids) > 500:
        raise HTTPException(422, '单次最多登记 500 张账单')
    invoice_ids = [int(i) for i in invoice_ids]
    if len(set(invoice_ids)) != len(invoice_ids):
        raise HTTPException(422, '账单号重复，请去重后再登记')

    _, ev = _get_evidence_result(db, batch_id)
    result = ev.result or {}
    if result.get('error'):
        raise HTTPException(422, '执行失败的批次没有可撤下的结果行')
    due_dates = load_due_dates(db)
    bilateral = wf.bilateral_invoice_ids(result)
    eligible = wf.eligible_invoice_ids(result, due_dates)

    not_bilateral = [i for i in invoice_ids if i not in bilateral]
    if not_bilateral:
        raise HTTPException(422, '账单 %s 未在两侧结果中同时带账单号，不能登记' % not_bilateral[:10])
    not_due = [i for i in invoice_ids if i not in eligible]
    if not_due:
        raise HTTPException(422, '账单 %s 尚未到期，不能撤下' % not_due[:10])

    created, skipped = [], []
    for inv in invoice_ids:
        existing = db.scalar(select(Withdrawal).where(
            Withdrawal.batch_id == batch_id, Withdrawal.invoice_id == inv))
        if existing:
            # 重试不新增登记；失败的登记重新排队
            if existing.status == 'failed':
                existing.status = 'pending'
                existing.fail_reason = None
                db.commit()
                enqueue(existing.id)
            skipped.append(serialize(existing))
            continue
        obj = Withdrawal(batch_id=batch_id, invoice_id=inv, status='pending')
        db.add(obj)
        db.commit()
        db.refresh(obj)
        enqueue(obj.id)
        created.append(serialize(obj))
    return {'created': created, 'deduplicated': skipped,
            'created_count': len(created), 'deduplicated_count': len(skipped)}


def retry_withdrawal(db, withdrawal_id):
    """失败登记重试：沿用同一条登记，状态重置为 pending，不新增行。"""
    obj = db.get(Withdrawal, withdrawal_id)
    if not obj:
        raise HTTPException(404, '撤下登记不存在')
    if obj.status == 'effective':
        return serialize(obj)
    obj.status = 'pending'
    obj.fail_reason = None
    db.commit()
    db.refresh(obj)
    enqueue(obj.id)
    return serialize(obj)


def effective_invoice_ids(db, batch_id):
    """某批次当前已生效撤下的账单号集合（按批次，供登记台账/单批次核对）。"""
    rows = db.scalars(select(Withdrawal.invoice_id).where(
        Withdrawal.batch_id == batch_id, Withdrawal.status == 'effective'))
    return {int(x) for x in rows}


def global_effective_invoice_ids(db):
    """全库已生效撤下的账单号集合。

    到期账单撤下一旦生效即全局有效：旧档与之后并发发布的复核，只要含该账单行，
    页面与下载都要剔除。登记仍按批次做双侧/到期校验，但过滤口径是全局的。
    """
    rows = db.scalars(select(Withdrawal.invoice_id).where(
        Withdrawal.status == 'effective').distinct())
    return {int(x) for x in rows}


def process_one(withdrawal_id, db=None):
    """worker 调用：逐张处理一条撤下登记。

    生效条件：登记仍存在、证据存在、账单当前仍到期且仍在两侧结果中。
    任何不满足都落 failed 并记录原因，可在页面看到失败原因后重试。
    db 可注入（测试/同连接场景）；生产 worker 默认自建独立 Session。
    """
    own_session = db is None
    if own_session:
        db = Session()
    try:
        obj = db.get(Withdrawal, withdrawal_id)
        if not obj or obj.status == 'effective':
            return
        obj.status = 'pending'
        db.commit()
        ev = db.scalar(select(Evidence).where(Evidence.batch_id == obj.batch_id))
        if not ev:
            obj.status, obj.fail_reason = 'failed', '复核证据不存在，无法撤下'
            db.commit()
            return
        result = ev.result or {}
        if result.get('error'):
            obj.status, obj.fail_reason = 'failed', '该批次执行失败，无结果行可撤下'
            db.commit()
            return
        due_dates = load_due_dates(db)
        if not wf.is_due(obj.invoice_id, due_dates):
            obj.status, obj.fail_reason = 'failed', '账单 %s 当前未到期' % obj.invoice_id
            db.commit()
            return
        if obj.invoice_id not in wf.bilateral_invoice_ids(result):
            obj.status, obj.fail_reason = 'failed', '账单 %s 未在两侧结果中同时出现' % obj.invoice_id
            db.commit()
            return
        # 幂等：确认可剔除后落 effective。实际过滤在读取时统一进行，
        # 不改写证据本身，保证旧档原始结果仍可查、撤下可叠加。
        obj.status = 'effective'
        obj.fail_reason = None
        db.commit()
    finally:
        if own_session:
            db.close()


def recover_pending():
    """启动/空轮询恢复：把所有 pending 登记重新入队，中断后不丢登记。"""
    with Session() as db:
        ids = list(db.scalars(select(Withdrawal.id).where(Withdrawal.status == 'pending')))
    for ident in ids:
        enqueue(ident)
    return ids
