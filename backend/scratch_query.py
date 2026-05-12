import asyncio
from app.core.db import SessionLocal
from app.models.models import PolyTrade, BotLog
from sqlalchemy import select

async def main():
    async with SessionLocal() as s:
        logs = (await s.execute(select(BotLog).order_by(BotLog.id.desc()).limit(20))).scalars().all()
        print('Recent Logs:')
        for l in reversed(logs):
            print(f'  {l.level}: {l.message}')

if __name__ == "__main__":
    asyncio.run(main())
