from fastapi import APIRouter, Depends, Query
from ..config.database import get_db
from ..schemas.withdrawal import WithdrawalInput
from ..services.auth import current_user, writer
from ..services import withdrawal as svc
router = APIRouter(prefix='/withdrawals')


@router.get('')
def listing(status: str | None = Query(None), batch_id: int | None = Query(None),
            db=Depends(get_db), user=Depends(current_user)):
    """撤下登记台账：可按 pending/effective/failed 与批次过滤。"""
    return svc.list_withdrawals(db, status=status, batch_id=batch_id)


@router.get('/eligible/{batch_id}')
def eligible(batch_id: int, db=Depends(get_db), user=Depends(current_user)):
    """某复核证据下两侧都带账单号、且已到期、可登记撤下的账单。"""
    return svc.eligible_for_batch(db, batch_id)


@router.post('', status_code=202)
def create(data: WithdrawalInput, db=Depends(get_db), user=Depends(writer)):
    return svc.create_withdrawals(db, data.batch_id, data.invoice_ids)


@router.post('/{withdrawal_id}/retry', status_code=202)
def retry(withdrawal_id: int, db=Depends(get_db), user=Depends(writer)):
    return svc.retry_withdrawal(db, withdrawal_id)
