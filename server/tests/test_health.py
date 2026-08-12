def test_health_and_pings(client):
    assert client.get('/health').json()=={'status':'ok','database':'ok'}
    assert client.get('/').json()=={'success':True,'data':{'pong':True}}
    assert client.post('/',content=b'{"action":"ping"}').json()=={'success':True,'data':{'pong':True}}
