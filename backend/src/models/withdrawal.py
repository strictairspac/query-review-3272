from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,Integer,ForeignKey,Text,DateTime,func,UniqueConstraint
from datetime import datetime
from ..config.database import Base
class Withdrawal(Base):
    """逐单复核留档中对到期账单的撤下登记。

    一张登记对应“某个复核批次证据 + 某张两侧都带账单号的账单”。
    登记创建后进入 pending，由 worker 逐张处理为 effective 或 failed；
    重试沿用同一条登记，不新增行。
    """
    __tablename__='withdrawals'
    __table_args__=(UniqueConstraint('batch_id','invoice_id',name='uq_withdrawal_batch_invoice'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('batches.id'),index=True)
    invoice_id:Mapped[int]=mapped_column(Integer,index=True)
    status:Mapped[str]=mapped_column(String(20),default='pending',index=True)
    fail_reason:Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    updated_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),onupdate=func.now())
