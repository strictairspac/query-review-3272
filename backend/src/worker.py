import time, json, traceback
from pathlib import Path
from sqlalchemy import select
from .config.database import Session
from .models.batch import Batch
from .models.sample import Sample
from .models.evidence import Evidence
from .models.withdrawal import Withdrawal
from .services.batch import queue
from .services import withdrawal as wd
from .constants.enums import REVIEW_QUEUE, WITHDRAW_QUEUE
from .services.query_engine import compare
heartbeat = Path('/artifacts/worker.heartbeat')


def run_one(batch_id):
    with Session() as db:
        batch = db.get(Batch, batch_id)
        if not batch or db.scalar(select(Evidence).where(Evidence.batch_id == batch_id)):
            return
        sample = db.get(Sample, batch.sample_id)
        batch.status = 'running'
        db.commit()
        try:
            result = compare(sample.original_sql, batch.candidate_sql)
            batch.status = 'passed' if result['equivalent'] else 'different'
        except Exception as exc:
            result = {'error': str(exc)[:500]}
            batch.status = 'failed'
        db.add(Evidence(batch_id=batch_id, result=result))
        db.commit()
        Path(f'/artifacts/review-{batch_id}.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2))


def drain_review(item):
    run_one(int(item[1]))


def drain_withdrawal(item):
    wd.process_one(int(item[1]))


def tick():
    """一轮队列处理：优先复核，再撤下；两者皆空时做中断恢复。返回是否处理了任务。"""
    item = queue.blpop(REVIEW_QUEUE, timeout=1)
    if item:
        drain_review(item)
        return True
    witem = queue.blpop(WITHDRAW_QUEUE, timeout=1)
    if witem:
        drain_withdrawal(witem)
        return True
    # 空轮询做状态恢复：中断遗留的复核任务与 pending 撤下重新处理
    with Session() as db:
        pending = list(db.scalars(
            select(Batch.id).where(Batch.status.in_(['queued', 'running']))))
    for ident in pending:
        run_one(ident)
    wd.recover_pending()
    return False


def main():
    while True:
        try:
            heartbeat.write_text(str(time.time()))
            tick()
        except Exception:
            traceback.print_exc()
            time.sleep(2)


if __name__ == '__main__':
    main()
