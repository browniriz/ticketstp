import pytest
from conftest import add_role,call

@pytest.mark.asyncio
async def test_role_matrix_and_admins(app,client):
    await add_role(app,1,'Иван','сотрудник',['Касса','Списание'])
    await add_role(app,2,'Админ','админ')
    role=call(client,'getRole',1)['data']
    assert role=={'role':'сотрудник','name':'Иван','allowedTypes':['Касса']}
    assert call(client,'getAdmins',2)['data']['admins']==[{'tg_id':'2','name':'Админ'}]
    assert call(client,'getAdmins',1)['success'] is False
