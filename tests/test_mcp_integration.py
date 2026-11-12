"""Independent stdio SDK handshake through a real local HTTP listener (opt-in)."""
import asyncio
import os
import socket
import sys
import threading
import time
from datetime import date
import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tollgate.platform_server import create_app
from tollgate.storage.migrate import upgrade
from tollgate.settings import Settings
from tollgate.jobs.worker import Worker

pytestmark = pytest.mark.skipif(os.environ.get('TOLLGATE_RUN_MCP_INTEGRATION') != '1', reason='Set TOLLGATE_RUN_MCP_INTEGRATION=1 for the independent local MCP handshake')


def test_independent_mcp_handshake_submission_poll_and_revoke(tmp_path):
    url='sqlite:///'+str(tmp_path/'mcp.sqlite');upgrade(url)
    app=create_app(Settings(database_url=url),local_token='fixture-operator')
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':'Bearer fixture-operator'}) as operator:
            workspace=operator.post('/api/v1/demo-workspaces',json={'request_id':'mcp-test'}).json()['id']
            path='/api/v1/workspaces/'+workspace
            mid=operator.post(path+'/mandates',json={'invoice_ids':['INV-1042']}).json()['id']
            credential=operator.post(path+'/agent-credentials',json={'mandate_id':mid}).json()
            async def exercise():
                parameters=StdioServerParameters(command=sys.executable,args=['-m','tollgate.integrations.mcp'],env={
                    'TOLLGATE_BASE_URL':f'http://127.0.0.1:{port}', 'TOLLGATE_WORKSPACE_ID':workspace,
                    'TOLLGATE_MANDATE_ID':mid,'TOLLGATE_AGENT_TOKEN':credential['agent_token']})
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        tools=await session.list_tools();names={t.name for t in tools.tools}
                        assert names=={'list_approved_invoices','get_approved_invoice','list_permitted_vendors','propose_invoice_payment','get_proposal_status','get_authorized_receipt'}
                        invoices=await session.call_tool('list_approved_invoices',{})
                        assert not invoices.isError
                        args=dict(request_id='external-run:call1',invoice_id='INV-1042',amount_minor=420000,beneficiary_ref='V-ACME',scheduled_date=date.today().isoformat())
                        result=await session.call_tool('propose_invoice_payment',args)
                        assert not result.isError
                        data=result.structuredContent
                        assert data['state']=='evaluating' and not data['executed']
                        retry=await session.call_tool('propose_invoice_payment',args)
                        assert retry.structuredContent['id']==data['id']
                        for _ in range(5): Worker(app.state.platform).tick()
                        final=await session.call_tool('get_proposal_status',{'proposal_id':data['id']})
                        assert final.structuredContent['state']=='confirmed'
                        receipt=await session.call_tool('get_authorized_receipt',{'receipt_id':final.structuredContent['receipt_id']})
                        assert receipt.structuredContent['payload']['kind']=='mock_payment'
                        bad=await session.call_tool('propose_invoice_payment',{**args,'amount_minor':420001})
                        assert bad.isError
                        operator.post(path+'/agent-credentials/'+credential['id']+'/revoke')
                        revoked=await session.call_tool('get_proposal_status',{'proposal_id':data['id']})
                        assert revoked.isError
            asyncio.run(exercise())
    finally:
        server.should_exit=True;thread.join(10);sock.close();app.state.platform.db.dispose()
