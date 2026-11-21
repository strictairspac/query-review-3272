from sqlalchemy import select
from fastapi import HTTPException
from ..models.evidence import Evidence
from .withdrawal import global_effective_invoice_ids
from . import withdrawal_filter as wf


def _raw(db, batch_id):
    x = db.scalar(select(Evidence).where(Evidence.batch_id == batch_id))
    if not x:
        raise HTTPException(404, '证据尚未生成，请等待 worker 完成')
    return x


def _view(db, x):
    """证据读视图：在原始留档之上叠加全局生效的到期账单撤下。

    一张账单撤下生效后，旧档与并发复核只要含该账单行都会被剔除；原始结论
    (equivalent/error)、列、计划、耗时不变，原行数保留为 *_row_count_original。
    """
    result = x.result
    removed = global_effective_invoice_ids(db)
    view_result = wf.apply_withdrawals(result, removed) if removed else result
    return {'id': x.id, 'batch_id': x.batch_id, 'result': view_result}


def list_evidence(db):
    return [_view(db, x) for x in db.scalars(select(Evidence).order_by(Evidence.id.desc()))]


def get_evidence(db, batch_id):
    return _view(db, _raw(db, batch_id))


def get_raw_evidence(db, batch_id):
    """未叠加撤下的原始留档，供需要核对原档时内部使用。"""
    x = _raw(db, batch_id)
    return {'id': x.id, 'batch_id': x.batch_id, 'result': x.result}
