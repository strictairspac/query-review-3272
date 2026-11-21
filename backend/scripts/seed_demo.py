from sqlalchemy import select,text
from src.config.database import Base,engine,Session
from src.models.user import User
from src.models.sample import Sample
from src.models.batch import Batch
from src.models.evidence import Evidence
from src.models.audit import Audit
from src.models.withdrawal import Withdrawal
from src.utils.password import hash_password
ORIGINAL="SELECT DISTINCT i.customer, (SELECT SUM(j.amount) FROM invoices j WHERE j.customer=i.customer AND j.temperature<=8) AS total FROM invoices i WHERE (SELECT SUM(k.amount) FROM invoices k WHERE k.customer=i.customer AND k.temperature<=8)>100 ORDER BY customer"
# 带账单号的明细样本：两侧都能逐单对上 invoice_id，供到期账单撤下登记使用
DETAIL_SQL="SELECT invoice_id, customer, route, amount FROM invoices WHERE status='settled' ORDER BY invoice_id"
def seed():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS invoices (invoice_id INTEGER PRIMARY KEY, customer TEXT NOT NULL, route TEXT NOT NULL, amount NUMERIC(12,2) NOT NULL, temperature NUMERIC(4,1) NOT NULL, status TEXT NOT NULL, due_date DATE)"))
        # 旧库升级：CREATE TABLE IF NOT EXISTS 不改已有表，这里补齐 due_date 列
        if engine.dialect.name == 'postgresql':
            cols=[r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='invoices'"))]
        else:
            cols=[r[1] for r in conn.execute(text("PRAGMA table_info(invoices)"))]
        if 'due_date' not in cols:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN due_date DATE"))
        if engine.dialect.name == 'postgresql':
            conn.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='query_reader') THEN CREATE ROLE query_reader NOLOGIN; END IF; END $$"))
            conn.execute(text('GRANT SELECT ON invoices TO query_reader'))
        # status 与到期日解耦：i%7==0 为 pending；i%5==0 的账单到期(2026-10-31，
        # 含 settled，使逐单明细里混有到期行)，其余未到期(2026-12-31)。
        conn.execute(text("INSERT INTO invoices SELECT i, (ARRAY['澄港海产','南港鲜食','北湾冷运'])[1+(i%3)], (ARRAY['东线','西线'])[1+(i%2)], 35+(i%17)*7, 2+(i%9), CASE WHEN i%7=0 THEN 'pending' ELSE 'settled' END, CASE WHEN i%5=0 THEN DATE '2026-10-31' ELSE DATE '2026-12-31' END FROM generate_series(1,600) i ON CONFLICT DO NOTHING"))
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email=role+'@chenggang.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,role=role,password_hash=hash_password('ColdChain!17')))
        samples=[('低温结算汇总','09:00 澄港海产、南港鲜食、北湾冷运的温控合格运费，600 行固定账单。',ORIGINAL),('路线平均费用','按路线观察已结算记录的均值；用 ROUND 保持两位小数。',"SELECT route, ROUND(AVG(amount),2) AS average FROM invoices WHERE status='settled' GROUP BY route ORDER BY route"),('异常温控账单','温度大于8度的账单，为差异复核提供反例。','SELECT customer, COUNT(*) AS count FROM invoices WHERE temperature > 8 GROUP BY customer ORDER BY customer'),('逐单结算明细','两侧都带账单号的明细留档，用于到期账单撤下登记与逐单核对。',DETAIL_SQL)]
        for name,desc,sql in samples:
            if not db.scalar(select(Sample).where(Sample.name==name)):db.add(Sample(name=name,description=desc,original_sql=sql))
        db.commit()
        return {t:db.execute(text('SELECT COUNT(*) FROM '+t)).scalar() for t in ('users','samples','batches','evidence','invoices','withdrawals')}
if __name__=='__main__':print(seed())
