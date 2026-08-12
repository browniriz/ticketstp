import base64
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import TOKEN, add_role, call, signed
from ticketsbot.app import create_app
from ticketsbot.config import Settings
from ticketsbot.models import Attachment


BASE={'type':'Касса','city':'Пермь','office':'1','name':'Иван','description':'Тест'}


@pytest.mark.asyncio
async def test_media_url_expiry_tamper_authorization_and_path_containment(app, client, tmp_path):
    app.state.settings.media_dir=tmp_path/'media'
    await add_role(app,1,'Автор'); await add_role(app,2,'Чужой'); await add_role(app,3,'Админ','админ')
    image='data:image/png;base64,'+base64.b64encode(b'secret').decode()
    ticket=call(client,'createTicket',1,**BASE,image=image,filename='x.png')['data']['ticket']
    token=ticket['fileUrl'].rsplit('/',1)[-1]

    assert client.post('/media/session',json={'token':token,'init_data':signed(2)}).status_code==404
    author_url=client.post('/media/session',json={'token':token,'init_data':signed(1)}).json()['url']
    admin_url=client.post('/media/session',headers={'X-Telegram-Init-Data':signed(3)},json={'token':token}).json()['url']
    assert 'init_data' not in author_url and client.get(author_url).content==b'secret'
    assert client.get(admin_url).status_code==200

    parsed=urlparse(author_url); query=parse_qs(parsed.query)
    query['sig']=['0'*64]
    tampered=urlunparse(parsed._replace(query=urlencode({k:v[0] for k,v in query.items()})))
    assert client.get(tampered).status_code==404
    query=parse_qs(parsed.query); query['expires']=[str(int(time.time())-1)]
    expired=urlunparse(parsed._replace(query=urlencode({k:v[0] for k,v in query.items()})))
    assert client.get(expired).status_code==404

    async with app.state.db.session() as session:
        attachment=(await session.execute(select(Attachment).where(Attachment.token==token))).scalar_one()
        attachment.stored_name='../outside.png'; await session.commit()
    (tmp_path/'outside.png').write_bytes(b'outside')
    assert client.get(author_url).status_code==404


def test_huge_body_is_rejected_before_json_decode(tmp_path):
    app=create_app(Settings(bot_token=TOKEN,database_url=f"sqlite+aiosqlite:///{tmp_path/'body.db'}",max_request_body_bytes=128))
    with TestClient(app) as client:
        response=client.post('/',content=b'{' + b'x'*1000)
        assert response.status_code==413


def test_text_field_length_bound(client):
    response=client.post('/',json={'action':'getRole','init_data':signed(1),'q':'x'*10001}).json()
    assert response['success'] is False and 'длину' in response['error']


def test_https_public_url_required_outside_localhost(tmp_path):
    app=create_app(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path/'bad.db'}",public_base_url='http://tickets.example.com'))
    with pytest.raises(ValueError,match='HTTPS'):
        with TestClient(app):
            pass
    good=create_app(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path/'good.db'}",public_base_url='https://tickets.example.com'))
    with TestClient(good) as client:
        assert client.get('/health').status_code==200