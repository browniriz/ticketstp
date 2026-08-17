import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from conftest import add_role, call, signed
from ticketsbot.attachments import safe_filename
from ticketsbot.models import Attachment, NotificationOutbox, Role, SheetSyncOutbox, Ticket
from ticketsbot.workers import ROLE_HEADERS, WorkerManager, ticket_row

BASE={'type':'Касса','city':'Пермь','office':'1','name':'Иван','description':'Тест'}


@pytest.mark.asyncio
async def test_attachment_create_authorized_download_and_replace(app,client,tmp_path):
    app.state.settings.media_dir=tmp_path/'media'
    await add_role(app,1,'Иван'); await add_role(app,2,'A','админ')
    image='data:image/png;base64,'+base64.b64encode(b'png-data').decode()
    result=call(client,'createTicket',1,**BASE,image=image,filename='safe\u202egnp.png')
    assert result['success']; ticket=result['data']['ticket']; token=ticket['fileUrl'].split('/')[-1]
    assert client.get('/media/'+token).status_code==404
    session=client.post('/media/session',json={'token':token,'init_data':signed(1)})
    assert session.status_code==200 and 'init_data' not in session.json()['url']
    response=client.get(session.json()['url'])
    assert response.status_code==200 and response.content==b'png-data' and response.headers['x-content-type-options']=='nosniff'
    replaced=call(client,'addScreenshot',2,number=ticket['number'],image=image,filename='new.png')
    assert replaced['success'] and replaced['data']['ticket']['fileUrl']!=ticket['fileUrl']
    assert client.get(session.json()['url']).status_code==404


def test_filename_and_blocked_attachment(app,client):
    assert '\u202e' not in safe_filename('a\u202egnp.exe')


@pytest.mark.asyncio
async def test_blocked_attachment_does_not_create_ticket(app,client):
    await add_role(app,1,'Иван')
    image='data:text/html;base64,'+base64.b64encode(b'<script/>').decode()
    assert not call(client,'createTicket',1,**BASE,image=image,filename='x.html')['success']
    async with app.state.db.session() as s:
        assert not list((await s.execute(select(Ticket))).scalars())


class FakeTelegram:
    def __init__(self): self.sent=[]
    async def send_message(self,*args): self.sent.append(args)


class SlowTelegram(FakeTelegram):
    async def send_message(self,*args):
        await asyncio.sleep(.05)
        await super().send_message(*args)


class FailingTelegram:
    async def send_message(self,*args): raise RuntimeError('telegram down')


class FakeBridge:
    def __init__(self): self.calls=[]
    async def call(self,action,**payload):
        self.calls.append((action,payload))
        if action=='bridgePullRoles':
            rows=[['9','Nine','админ','nine','']+[True]*9]
            canonical=json.dumps({'headers':ROLE_HEADERS,'rows':rows},ensure_ascii=False,separators=(',',':'))
            return {'headers':ROLE_HEADERS,'rows':rows,'revision':1,'count':1,
                    'hash':hashlib.sha256(canonical.encode()).hexdigest()}
        if action=='bridgeUpsertTicket':
            return {'number':payload['row'][0],'sequence':payload['sequence'],
                    'dedupe_key':payload['dedupe_key']}
        if action=='bridgeMirrorAccess':
            item=payload['payload']
            return {'tg_id':str(item.get('tg_id') or item.get('creator_id') or ''),
                    'operation':payload['operation'],'sequence':item.get('sequence'),
                    'dedupe_key':payload['dedupe_key']}
        return {}


@pytest.mark.asyncio
async def test_worker_start_does_not_pull_roles_from_google(app):
    worker = WorkerManager(app.state.db, app.state.settings, bridge=FakeBridge())
    worker.start()
    try:
        names = {task.get_name() for task in worker.tasks}
        assert "roles-pull" not in names
        assert "sheet-outbox" in names
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_injectable_workers_delivery_roles_and_exact_columns(app,client):
    await add_role(app,1,'Иван')
    call(client,'createTicket',1,**BASE)
    tg=FakeTelegram(); bridge=FakeBridge(); worker=WorkerManager(app.state.db,app.state.settings,tg,bridge)
    await worker.notifications_once(); await worker.sheets_once(); await worker.pull_roles_once()
    async with app.state.db.session() as s:
        assert all(x.delivered for x in (await s.execute(select(NotificationOutbox))).scalars())
        assert all(x.delivered for x in (await s.execute(select(SheetSyncOutbox))).scalars())
        assert (await s.get(Role,'9')).role=='админ' and await s.get(Role,'1') is None
        ticket=(await s.execute(select(Ticket))).scalar_one()
        assert len(ticket_row(ticket))==18
    assert tg.sent and any(x[0]=='bridgeUpsertTicket' and len(x[1]['row'])==18 for x in bridge.calls)


@pytest.mark.asyncio
async def test_concurrent_workers_atomically_claim_notification(app,client):
    await add_role(app,1,'Иван'); call(client,'createTicket',1,**BASE)
    tg=SlowTelegram()
    first=WorkerManager(app.state.db,app.state.settings,tg,FakeBridge())
    second=WorkerManager(app.state.db,app.state.settings,tg,FakeBridge())
    await asyncio.gather(first.notifications_once(),second.notifications_once())
    assert len(tg.sent)==1
    async with app.state.db.session() as s:
        row=(await s.execute(select(NotificationOutbox))).scalar_one()
        assert row.delivered and row.attempts==1 and not row.claim_token and row.claimed_at is None


@pytest.mark.asyncio
async def test_worker_retry_backoff_and_terminal_failure_visible(app,client):
    app.state.settings.worker_max_attempts=1
    await add_role(app,1,'Иван'); call(client,'createTicket',1,**BASE)
    await WorkerManager(app.state.db,app.state.settings,FailingTelegram(),FakeBridge()).notifications_once()
    async with app.state.db.session() as s:
        row=(await s.execute(select(NotificationOutbox))).scalar_one()
        assert not row.delivered and row.attempts==1
        now = datetime.now(timezone.utc)
        retry_at = row.next_attempt_at.replace(tzinfo=timezone.utc)
        assert retry_at >= now + timedelta(seconds=25)
        assert 'permanently failed after 1 attempts' in row.last_error
    assert WorkerManager(app.state.db, app.state.settings, bridge=FakeBridge()).bridge is not None
    from ticketsbot.workers import BridgeClient
    assert BridgeClient.REQUEST_TIMEOUT_SECONDS == 120


class MismatchedAckBridge(FakeBridge):
    async def call(self,action,**payload):
        if action=='bridgeUpsertTicket':
            return {'number':'Z999','dedupe_key':payload['dedupe_key']}
        return await super().call(action,**payload)


@pytest.mark.asyncio
async def test_sheet_worker_mismatched_ack_remains_pending(app,client):
    await add_role(app,1,'Иван'); call(client,'createTicket',1,**BASE)
    await WorkerManager(app.state.db,app.state.settings,bridge=MismatchedAckBridge()).sheets_once()
    async with app.state.db.session() as s:
        rows=list((await s.execute(select(SheetSyncOutbox))).scalars())
        assert rows and all(not row.delivered for row in rows)
        assert all('mismatched ticket bridge acknowledgment' in row.last_error for row in rows)


class NewerSnapshotBridge(FakeBridge):
    async def call(self, action, **payload):
        if action == 'bridgeUpsertTicket' and payload['sequence'] == 1:
            raise RuntimeError('bridge: stale ticket sequence')
        return await super().call(action, **payload)


@pytest.mark.asyncio
async def test_newer_ticket_snapshot_supersedes_failed_older_intents(app, client):
    await add_role(app, 1, 'Иван')
    await add_role(app, 2, 'Админ', 'админ')
    ticket = call(client, 'createTicket', 1, **BASE)['data']['ticket']
    assert call(client, 'takeTicket', 2, number=ticket['number'])['success']
    await WorkerManager(app.state.db, app.state.settings, bridge=NewerSnapshotBridge()).sheets_once()
    async with app.state.db.session() as session:
        rows = list((await session.execute(select(SheetSyncOutbox).order_by(
            SheetSyncOutbox.id))).scalars())
        assert len(rows) == 2
        assert all(row.delivered and not row.last_error for row in rows)
