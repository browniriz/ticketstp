import pytest
from sqlalchemy import func, select

from conftest import add_role,call
from ticketsbot.models import NotificationOutbox, SheetSyncOutbox

@pytest.mark.asyncio
async def test_access_lifecycle(app,client):
    await add_role(app,9,'Админ','админ')
    assert call(client,'getRole',5)['data']['pending'] is False
    assert call(client,'requestAccess',5,name='Пётр')['data']=={'ok':True}
    assert call(client,'getRole',5)['data']['pending'] is True
    access=call(client,'getAccess',9)['data']; assert access['requests'][0]['tg_id']=='5'
    assert call(client,'approveAccess',9,target_tg_id='5',target_name='Пётр')['success']
    assert call(client,'renameRole',9,target_tg_id='5',target_name='Петр П.')['data']['name']=='Петр П.'
    assert call(client,'revokeAccess',9,target_tg_id='5')['success']
    assert call(client,'getRole',5)['data']['role']=='гость'


@pytest.mark.asyncio
async def test_access_outboxes_allow_repeated_lifecycle_and_role_changes(app,client):
    await add_role(app,9,'Админ','админ')

    def succeeds(action, **body):
        result=call(client,action,5 if action=='requestAccess' else 9,**body)
        assert result['success'], result

    # Values and transitions may legitimately recur over the lifetime of a role.
    # Each API operation must create an independently deliverable outbox event.
    succeeds('requestAccess',name='Пётр')
    succeeds('approveAccess',target_tg_id='5',target_name='Пётр')
    succeeds('renameRole',target_tg_id='5',target_name='Петр П.')
    succeeds('renameRole',target_tg_id='5',target_name='Пётр')
    succeeds('renameRole',target_tg_id='5',target_name='Петр П.')
    succeeds('revokeAccess',target_tg_id='5')

    succeeds('requestAccess',name='Пётр')
    succeeds('approveAccess',target_tg_id='5',target_name='Пётр')
    succeeds('revokeAccess',target_tg_id='5')
    succeeds('requestAccess',name='Пётр')
    succeeds('approveAccess',target_tg_id='5',target_name='Пётр')

    assert call(client,'getRole',5)['data']['role']=='сотрудник'
    async with app.state.db.session() as session:
        sheet_counts=dict((await session.execute(
            select(SheetSyncOutbox.operation,func.count()).group_by(SheetSyncOutbox.operation)
        )).all())
        notification_count=await session.scalar(select(func.count()).select_from(NotificationOutbox))
        sheet_keys=list((await session.execute(select(SheetSyncOutbox.dedupe_key))).scalars())
        notification_keys=list((await session.execute(select(NotificationOutbox.dedupe_key))).scalars())

    assert sheet_counts=={'approve':3,'rename':3,'request':3,'revoke':2}
    assert notification_count==8  # request, approval, and revocation notifications
    assert len(sheet_keys)==len(set(sheet_keys))
    assert len(notification_keys)==len(set(notification_keys))
