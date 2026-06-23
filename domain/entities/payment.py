from datetime import datetime
from pydantic import BaseModel, Field

class PaymentEntity(BaseModel):
    transaction_id: str
    amount: float = Field(gt=0)
    currency: str = "INR"
    status: str = "PENDING"
    created_at: datetime = Field(default_factory=datetime.utcnow)
