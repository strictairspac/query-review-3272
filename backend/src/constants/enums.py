ROLES=('admin','operator','viewer')
STATUSES=('queued','running','passed','different','failed')
# 撤下登记的处理状态：登记后排队，worker 逐张账单处理
WITHDRAW_STATUSES=('pending','effective','failed')
# 复核队列
REVIEW_QUEUE='query-review:jobs'
# 账单撤下队列
WITHDRAW_QUEUE='query-review:withdrawals'
