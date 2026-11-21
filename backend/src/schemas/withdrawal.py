from pydantic import BaseModel, Field


class WithdrawalInput(BaseModel):
    batch_id: int = Field(gt=0)
    invoice_ids: list[int] = Field(min_length=1, max_length=500)


class WithdrawalRetry(BaseModel):
    pass
