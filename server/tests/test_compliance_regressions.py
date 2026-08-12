import concurrent.futures
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from conftest import add_role, call
from ticketsbot.app import create_app
from ticketsbot.config import Settings
from ticketsbot.models import NotificationOutbox, SheetSyncOutbox, Ticket, TicketEvent

BASE={'type':'Касса','city':'Пермь','office':'1','name':'Иван','description':'Тест'}


def test_configurable_cors_and_options(tmp_path):
    from fastapi.testclient import TestClient
    app=create_app(Settings(bot_token='123:test-token',database_url=f"sqlite+aiosqlite:///{tmp_path/'cors.db'}",cors_origins='https://front.example'))
    with TestClient(app) as client:
        r=client.options('/',headers={'Origin':'https://front.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
        assert r.status_code==200
        assert r.headers['access-control-allow-origin']=='https://front.example'
        assert 'POST' in r.headers['access-control-allow-methods']


@pytest.mark.asyncio
async def test_atomic_events_outboxes_and_secret_free_payloads(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'A','админ'); await add_role(app,3,'B','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']
    call(client,'takeTicket',2,number=number)
    call(client,'transferTicket',2,number=number,to_tg_id='3')
    call(client,'returnTicket',3,number=number,reason='fix')
    call(client,'resubmitTicket',1,number=number,**BASE)
    async with app.state.db.session() as s:
        events=list((await s.execute(select(TicketEvent).order_by(TicketEvent.id))).scalars())
        assert {'create','take','transfer','на доработке','resubmit'} <= {e.event for e in events}
        sheets=list((await s.execute(select(SheetSyncOutbox))).scalars())
        notifications=list((await s.execute(select(NotificationOutbox))).scalars())
        assert len(sheets)>=5 and len(notifications)>=5
        payload=' '.join(x.payload_json for x in sheets).lower()
        assert 'init_data' not in payload and 'test-token' not in payload and 'hash' not in payload
        assert all(not x.delivered for x in sheets+notifications)


@pytest.mark.asyncio
async def test_finish_empty_comment_clears_revision_reason(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'A','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']
    call(client,'takeTicket',2,number=number); call(client,'returnTicket',2,number=number,reason='old')
    call(client,'resubmitTicket',1,number=number,**BASE); call(client,'takeTicket',2,number=number)
    result=call(client,'finishTicket',2,number=number,comment='')
    assert result['data']['ticket']['reason']==''


@pytest.mark.asyncio
async def test_resubmit_and_transfer_are_cas_safe(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'A','админ'); await add_role(app,3,'B','админ'); await add_role(app,4,'C','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']; call(client,'takeTicket',2,number=number)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda uid: call(client,'transferTicket',2,number=number,to_tg_id=str(uid)),[3,4]))
    assert sum(x['success'] for x in results)>=1
    # Every successful transfer is its own CAS transition; depending on SQLite
    # scheduling the second request can legitimately observe the first commit.
    async with app.state.db.session() as s:
        ticket=(await s.execute(select(Ticket).where(Ticket.number==number))).scalar_one()
        assert ticket.version==2+sum(x['success'] for x in results)
        current_admin=int(ticket.admin_id)
    call(client,'returnTicket',current_admin,number=number,reason='fix')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: call(client,'resubmitTicket',1,number=number,**BASE),range(2)))
    assert sum(x['success'] for x in results)==1


@pytest.mark.asyncio
async def test_rate_limits(app,client):
    await add_role(app,1,'Иван')
    for _ in range(30): assert call(client,'createTicket',1,**BASE)['success']
    assert not call(client,'createTicket',1,**BASE)['success']
    for _ in range(5): assert call(client,'requestAccess',50,name='Guest')['success']
    assert not call(client,'requestAccess',50,name='Guest')['success']


class FakeTelegram:
    async def get_chat(self,tg_id):
        if str(tg_id)=='2': return {'username':'updated_user'}
        raise RuntimeError('not found')


@pytest.mark.asyncio
async def test_refresh_contacts_reports_actual_updated_failed(tmp_path):
    from fastapi.testclient import TestClient
    from conftest import TOKEN
    app=create_app(Settings(bot_token=TOKEN,database_url=f"sqlite+aiosqlite:///{tmp_path/'tg.db'}"),telegram_client=FakeTelegram())
    with TestClient(app) as client:
        await add_role(app,1,'Admin','админ'); await add_role(app,2,'Ok'); await add_role(app,3,'Fail')
        result=call(client,'refreshContacts',1)
        assert result['data']=={'updated':1,'failed':1}
        async with app.state.db.session() as s: assert (await s.get(__import__('ticketsbot.models',fromlist=['Role']).Role,'2')).username=='updated_user'


@pytest.mark.asyncio
async def test_js_rounding_semantics(app,client):
    await add_role(app,2,'A','админ')
    async with app.state.db.session() as s:
        now=datetime.now(timezone.utc)
        for i,elapsed in enumerate((2,3)):
            s.add(Ticket(number=f'R{i:03}',created_at=now,type='Касса',city='Пермь',office='1',sender_name='x',description='x',status='решена',creator_id='9',admin_id='2',admin_name='A',elapsed_seconds=elapsed,resolved_at=now,updated_at=now))
        await s.commit()
    assert call(client,'getLeaderboard',2)['data']['leaders'][0]['avgSeconds']==3


@pytest.mark.asyncio
async def test_month_boundary_uses_yekaterinburg(monkeypatch,app,client):
    import ticketsbot.services.tickets as service
    await add_role(app,2,'A','админ')
    monkeypatch.setattr(service,'utcnow',lambda: datetime(2026,1,15,tzinfo=timezone.utc))
    async with app.state.db.session() as s:
        resolved=datetime(2025,12,31,20,0,tzinfo=timezone.utc)
        s.add(Ticket(number='TZ01',created_at=resolved,type='Касса',city='Пермь',office='1',sender_name='x',description='x',status='решена',creator_id='9',admin_id='2',admin_name='A',elapsed_seconds=1,resolved_at=resolved,updated_at=resolved))
        await s.commit()
    assert call(client,'getLeaderboard',2,period='month')['data']['leaders'][0]['count']==1


def test_versioned_migration_upgrades_existing_database(tmp_path):
    import asyncio
    import sqlite3
    from fastapi.testclient import TestClient
    path=tmp_path/'old.db'; conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE tickets (id INTEGER PRIMARY KEY, number VARCHAR(4), status VARCHAR(30))')
    conn.execute("INSERT INTO tickets(id,number,status) VALUES (1,'L001','создана')")
    conn.commit(); conn.close()
    app=create_app(Settings(database_url=f'sqlite+aiosqlite:///{path}'))
    with TestClient(app): pass
    conn=sqlite3.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==6
    assert list(tmp_path.glob('old.db.pre-migration-*.bak'))
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='rate_limits'").fetchone()[0]==1
    columns={row[1] for row in conn.execute('PRAGMA table_info(tickets)')}
    assert set(Ticket.__table__.columns.keys()) <= columns
    indexes={row[1] for row in conn.execute('PRAGMA index_list(tickets)')}
    assert {'ix_tickets_number','ix_tickets_status','ix_tickets_creator_id'} <= indexes
    conn.close()

    app=create_app(Settings(bot_token='123:test-token',database_url=f'sqlite+aiosqlite:///{path}',notify_chat_id='-100'))
    with TestClient(app) as client:
        asyncio.run(add_role(app,1,'Иван'))
        asyncio.run(add_role(app,2,'Admin','админ'))
        legacy=call(client,'getTickets',2)
        assert legacy['success'] and legacy['data']['tickets'][0]['number']=='L001'
        assert call(client,'takeTicket',2,number='L001')['success']
        assert call(client,'pauseTicket',2,number='L001')['success']
        assert call(client,'resumeTicket',2,number='L001')['success']
        assert call(client,'finishTicket',2,number='L001',comment='migrated')['success']
        created=call(client,'createTicket',1,**BASE)
        assert created['success'] and created['data']['ticket']['status']=='создана'


@pytest.mark.asyncio
async def test_notification_recipients_thread_and_semantics(app,client):
    import json
    await add_role(app,1,'Иван'); await add_role(app,2,'A','админ'); await add_role(app,3,'B','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']
    call(client,'takeTicket',2,number=number)
    call(client,'pauseTicket',2,number=number)
    call(client,'resumeTicket',2,number=number)
    call(client,'transferTicket',2,number=number,to_tg_id='3')
    call(client,'finishTicket',3,number=number,comment='готово')
    async with app.state.db.session() as s:
        rows=list((await s.execute(select(NotificationOutbox).order_by(NotificationOutbox.id))).scalars())
    work=[r for r in rows if r.chat_id=='-100900']
    assert work and all(r.thread_id=='43695' for r in work)
    assert any('Новая заявка' in r.text and 'Пермь / 1' in r.text for r in work)
    assert any('снова в работе' in r.text for r in work)
    assert any('передана: A → B' in r.text for r in work)
    assert any('Затрачено:' in r.text and 'Исполнитель: B' in r.text and 'Комментарий: готово' in r.text for r in work)
    assert any(r.chat_id=='3' and 'Вам передали' in r.text for r in rows)
    assert any(r.chat_id=='1' and 'решена' in r.text and 'готово' in r.text for r in rows)
    assert all(json.loads(r.payload_json)['text']==r.text for r in rows)


@pytest.mark.asyncio
async def test_non_owner_admin_can_transfer_active_ticket(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'Owner','админ')
    await add_role(app,3,'Other','админ'); await add_role(app,4,'Target','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']
    call(client,'takeTicket',2,number=number)
    result=call(client,'transferTicket',3,number=number,to_tg_id='4')
    assert result['success']
    assert result['data']['ticket']['adminId']=='4'
