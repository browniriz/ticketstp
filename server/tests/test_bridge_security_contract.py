import hashlib
import json
from pathlib import Path

import httpx
import pytest

from ticketsbot.workers import BridgeClient, ROLE_HEADERS, WorkerManager


def snapshot(rows, revision=1, *, count=None, digest=None):
    canonical=json.dumps({'headers':ROLE_HEADERS,'rows':rows},ensure_ascii=False,separators=(',',':'))
    return {'headers':ROLE_HEADERS,'rows':rows,'revision':revision,
            'count':len(rows) if count is None else count,
            'hash':digest or hashlib.sha256(canonical.encode()).hexdigest()}


class Bridge:
    def __init__(self, data): self.data=data
    async def call(self, *_args, **_kwargs): return self.data


@pytest.mark.asyncio
async def test_snapshot_hash_count_and_stale_revision_rejected(app):
    await app.state.db.initialize()
    rows=[['9','Admin','админ','','']+[True]*9]
    worker=WorkerManager(app.state.db,app.state.settings,bridge=Bridge(snapshot(rows,2)))
    await worker.pull_roles_once()
    worker.bridge=Bridge(snapshot(rows,1))
    with pytest.raises(ValueError,match='stale'): await worker.pull_roles_once()
    worker.bridge=Bridge(snapshot(rows,3,count=2))
    with pytest.raises(ValueError,match='integrity'): await worker.pull_roles_once()


def test_bridge_url_allowlist_and_formula_contract():
    BridgeClient('https://script.google.com/macros/s/x/exec','s')
    with pytest.raises(ValueError): BridgeClient('http://script.google.com/x','s')
    with pytest.raises(ValueError): BridgeClient('https://evil.example/x','s')
    source=(Path(__file__).parents[2]/'Code.gs').read_text(encoding='utf-8')
    assert 'function plainText_' in source and 'sanitizeBridgeTicketRow_' in source
    assert 'stale access sequence' in source and 'cannot delete last admin' in source


@pytest.mark.asyncio
async def test_redirect_does_not_forward_secret(monkeypatch):
    requests=[]
    async def handler(request):
        requests.append(request)
        if request.method=='POST':
            return httpx.Response(302,headers={'location':'https://script.googleusercontent.com/macros/echo?x=1'})
        return httpx.Response(200,json={'success':True,'data':{}})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    await BridgeClient('https://script.google.com/macros/s/x/exec','top-secret').call('bridgePullRoles')
    assert len(requests)==2 and b'top-secret' in requests[0].content
    assert b'top-secret' not in requests[1].content and 'bridge_secret' not in requests[1].headers