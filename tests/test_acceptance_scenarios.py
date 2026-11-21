"""三个实测场景的串联验收（可直接运行观察输出）：
1) 混有未到期账单的旧档：只撤到期账单，未到期行保留；
2) 发布撞上撤下：并发复核候选侧丢失账单 -> 撤下落 failed 并给原因；
3) 重启：队列清空后 pending 登记被恢复并最终生效。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from src.services import withdrawal as wd
from src.services import evidence as ev
from src.models.withdrawal import Withdrawal
from tests.conftest import make_evidence

DETAIL = "SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' ORDER BY invoice_id"
DUE, DUE2, NOT_DUE = 5, 10, 1


@pytest.mark.usefixtures('fake_redis')
def test_acceptance_three_scenarios(session, samples):
    # 场景 1：混有未到期账单的旧档
    bid, _ = make_evidence(session, samples['detail'], DETAIL, 'mixed-old-archive')
    wd.create_withdrawals(session, bid, [DUE])
    wid = wd.list_withdrawals(session, status='pending')[0]['id']
    wd.process_one(wid, db=session)
    view = ev.get_evidence(session, bid)['result']
    ids = [int(r[0]) for r in view['original']['rows']]
    assert DUE not in ids and NOT_DUE in ids
    print('\n[场景1] 旧档混合到期/未到期 -> 撤下后到期账单 %d 已剔除，未到期账单 %d 保留，原行数 %s -> 当前 %d'
          % (DUE, NOT_DUE, view['original_row_count_original'], view['original']['row_count']))

    # 场景 2：发布撞上撤下
    bid2, result2 = make_evidence(session, samples['detail'], DETAIL, 'collision')
    wid2 = wd.create_withdrawals(session, bid2, [DUE2])['created'][0]['id']
    result2['candidate']['rows'] = [r for r in result2['candidate']['rows'] if int(r[0]) != DUE2]
    result2['candidate']['row_count'] = len(result2['candidate']['rows'])
    from sqlalchemy import select
    from src.models.evidence import Evidence
    ev2 = session.scalar(select(Evidence).where(Evidence.batch_id == bid2))
    ev2.result = {**result2}
    session.commit()
    wd.process_one(wid2, db=session)
    failed = session.get(Withdrawal, wid2)
    assert failed.status == 'failed'
    print('[场景2] 发布撞上撤下 -> 登记 #%d 状态 %s，原因：%s' % (wid2, failed.status, failed.fail_reason))

    # 场景 3：重启恢复
    bid3, _ = make_evidence(session, samples['detail'], DETAIL, 'restart')
    wid3 = wd.create_withdrawals(session, bid3, [DUE2])['created'][0]['id']
    wd.queue.flushall()  # 模拟重启丢队列
    recovered = wd.recover_pending()
    assert wid3 in recovered
    wd.process_one(wid3, db=session)
    assert session.get(Withdrawal, wid3).status == 'effective'
    print('[场景3] 重启 -> pending 登记 #%d 被恢复并生效，登记总数不增加（共 %d 条）'
          % (wid3, session.query(Withdrawal).count()))
