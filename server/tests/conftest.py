import hashlib, hmac, json, time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from ticketsbot.app import create_app
from ticketsbot.config import Settings
from ticketsbot.models import Role

TOKEN="123:test-token"


def signed(user_id, first_name="User", username="user", auth_date=None, **extra):
    fields={"auth_date":str(int(time.time()) if auth_date is None else auth_date),"query_id":"test","user":json.dumps({"id":user_id,"first_name":first_name,"username":username},separators=(",",":"),ensure_ascii=False),**extra}
    check="\n".join(f"{k}={fields[k]}" for k in sorted(fields)); secret=hmac.new(b"WebAppData",TOKEN.encode(),hashlib.sha256).digest(); fields["hash"]=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(bot_token=TOKEN,database_url=f"sqlite+aiosqlite:///{tmp_path/'test.db'}",
                               notify_chat_id='-100900',notify_thread_id='43695'))

@pytest.fixture
def client(app):
    with TestClient(app) as c: yield c


def call(client,action,user=1,**body):
    body.update(action=action,init_data=signed(user)); return client.post("/",content=json.dumps(body,ensure_ascii=False).encode()).json()


async def add_role(app,tg_id,name,role="сотрудник",allowed=None):
    async with app.state.db.session() as s:
        s.add(Role(tg_id=str(tg_id),name=name,role=role,allowed_types_json=json.dumps(allowed,ensure_ascii=False) if allowed is not None else None)); await s.commit()
