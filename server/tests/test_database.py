import pytest
from sqlalchemy import text
from ticketsbot.models import Role,Ticket,AccessRequest,TicketEvent,NotificationOutbox,SheetSyncOutbox,SyncState

@pytest.mark.asyncio
async def test_schema_wal_foreign_keys_integrity(app):
    await app.state.db.initialize()
    async with app.state.db.engine.connect() as c:
        assert (await c.execute(text('PRAGMA journal_mode'))).scalar_one()=='wal'
        assert (await c.execute(text('PRAGMA foreign_keys'))).scalar_one()==1
        names=set((await c.execute(text("select name from sqlite_master where type='table'"))).scalars())
    assert {'roles','access_requests','tickets','ticket_events','notification_outbox','sheet_sync_outbox','sync_state','bridge_sequences'}<=names
    assert await app.state.db.integrity_check()=='ok'
