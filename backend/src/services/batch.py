from sqlalchemy import select
from fastapi import HTTPException
from ..config.settings import REDIS_URL
from ..models.batch import Batch
from ..models.sample import Sample
from ..constants.enums import REVIEW_QUEUE
from .query_engine import validate_sql
try:
    from redis import Redis
    queue = Redis.from_url(REDIS_URL)
except Exception:  # 受限本地环境无 Redis 时的兜底
    queue = None


def _rpush(job_id):
    if queue is not None:
        queue.rpush(REVIEW_QUEUE, job_id)


def serialize(x):return {'id':x.id,'name':x.name,'sample_id':x.sample_id,'candidate_sql':x.candidate_sql,'status':x.status,'created_at':str(x.created_at)}
def list_batches(db):return [serialize(x) for x in db.scalars(select(Batch).order_by(Batch.id.desc()))]
def create_batch(db,data):
    validate_sql(data.candidate_sql)
    if not db.get(Sample,data.sample_id):raise HTTPException(404,'样本不存在')
    if db.scalar(select(Batch).where(Batch.name==data.name)):raise HTTPException(409,'批次名称已存在')
    if len(list(db.scalars(select(Batch).where(Batch.status.in_(['queued','running'])))))>=20:raise HTTPException(429,'待执行任务达到20个，请稍后重试')
    obj=Batch(**data.model_dump());db.add(obj);db.commit();_rpush(obj.id);return serialize(obj)
