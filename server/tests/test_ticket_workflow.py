import concurrent.futures
import pytest
from conftest import add_role,call

BASE={'type':'Касса','city':'Пермь','office':'1','name':'Иван','description':'Тест'}

@pytest.mark.asyncio
async def test_full_workflow_and_history(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'Админ','админ')
    created=call(client,'createTicket',1,**BASE); assert created['success']; number=created['data']['ticket']['number']
    assert call(client,'takeTicket',2,number=number)['data']['ticket']['status']=='в работе'
    assert call(client,'pauseTicket',2,number=number)['data']['ticket']['status']=='на паузе'
    assert call(client,'resumeTicket',2,number=number)['data']['ticket']['status']=='в работе'
    assert call(client,'returnTicket',2,number=number,reason='Исправить')['data']['ticket']['status']=='на доработке'
    assert call(client,'resubmitTicket',1,number=number,**BASE)['data']['ticket']['status']=='исправлена'
    assert call(client,'takeTicket',2,number=number)['data']['ticket']['status']=='в работе'
    assert call(client,'finishTicket',2,number=number,comment='Готово')['data']['ticket']['status']=='решена'
    history=call(client,'getHistory',2,q=number)['data']; assert history['total']==1 and history['tickets'][0]['reason']=='Готово'

@pytest.mark.asyncio
async def test_exactly_one_concurrent_take(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'A','админ'); await add_role(app,3,'B','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda uid: call(client,'takeTicket',uid,number=number),[2,3]))
    assert sum(bool(x['success']) for x in results)==1

@pytest.mark.asyncio
async def test_reject_and_leaderboard(app,client):
    await add_role(app,1,'Иван'); await add_role(app,2,'Админ','админ')
    number=call(client,'createTicket',1,**BASE)['data']['ticket']['number']
    assert call(client,'rejectTicket',2,number=number,reason='Нет')['data']['ticket']['status']=='отклонена'
    leaders=call(client,'getLeaderboard',2)['data']['leaders']; assert leaders[0]['count']==1
