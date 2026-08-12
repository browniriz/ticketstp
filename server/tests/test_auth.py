from conftest import call, signed
import time

def test_auth_rejects_bad_signature_and_ignores_tg_id(client):
    bad=client.post('/',content=b'{"action":"getRole","init_data":"x=y"}').json()
    assert bad['success'] is False
    result=client.post('/',json={'action':'getRole','init_data':signed(55),'tg_id':'999'}).json()
    assert result['data']['role']=='гость'


def test_auth_rejects_stale_and_future_signed_data(client):
    now=int(time.time())
    stale=client.post('/',json={'action':'getRole','init_data':signed(1,auth_date=now-86401)}).json()
    future=client.post('/',json={'action':'getRole','init_data':signed(1,auth_date=now+61)}).json()
    assert stale['success'] is False and 'устарела' in stale['error']
    assert future['success'] is False and 'будущем' in future['error']
