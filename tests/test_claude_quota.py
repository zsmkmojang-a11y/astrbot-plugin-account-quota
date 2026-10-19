import asyncio,json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).parent))
from test_quota import Plugin,Context,Event,quota
reader_type=Plugin.__init__.__globals__['ClaudeReader']
g=reader_type.fetch.__globals__
report=g['claude_report']
class ClaudeTests(unittest.IsolatedAsyncioTestCase):
 def test_report(self):
  text=report({'five_hour':{'utilization':25,'resets_at':'2026-10-20T00:00:00Z'},'seven_day':{'utilization':None}}).text
  self.assertIn('75%',text);self.assertIn('未知',text)
  for value in [True,-1,101,float('nan'),'25']:
   with self.assertRaises(Exception):report({'five_hour':{'utilization':value}})
  for value in [{},None,{'five_hour':[]},{'five_hour':{'resets_at':'bad'}}]:
   with self.assertRaises(Exception):report(value)
 async def test_disabled_never_calls_reader(self):
  p=Plugin(Context(),{'codex_query_enabled':False,'deepseek_query_enabled':False})
  try:
   p.claude_reader.fetch=AsyncMock(side_effect=AssertionError('must not query'))
   self.assertIn('关闭',await p._answer(Event(),'claude'))
   await p._answer(Event(),'all');p.claude_reader.fetch.assert_not_awaited()
  finally:await p.terminate()
 async def test_enabled_commands_mtk_natural_and_permission(self):
  p=Plugin(Context(),{'codex_query_enabled':False,'deepseek_query_enabled':False,'claude_query_enabled':True})
  try:
   p.claude_reader.fetch=AsyncMock(return_value=quota.QuotaReport('Claude remaining 75%'))
   for method,msg in [(p.claude_command,'claude额度'),(p.mtk_shortcut,'mtk')]:
    e=Event(message=msg,wake=False);await method(e);self.assertIn('75%',e.sent[0])
   self.assertEqual(p.claude_reader.fetch.await_count,1)
   self.assertEqual(quota.natural_target('帮我查一下Claude额度'),'claude')
   p.config['claude_query_enabled']=False
   self.assertIn('关闭',await p._answer(Event(),'claude'))
   p.config['claude_query_enabled']=True
   self.assertIn('管理员',await p._answer(Event(admin=False),'claude'))
  finally:await p.terminate()
 async def test_cli_not_logged_in_does_not_read_credentials(self):
  r=reader_type()
  with patch.dict(g,resolve_claude=lambda c:'claude',_run=AsyncMock(return_value=json.dumps({'loggedIn':False})),read_token=lambda:(_ for _ in ()).throw(AssertionError('must not read'))):
   with self.assertRaises(Exception) as err:await r.fetch('claude',5)
   self.assertIn('auth login',str(err.exception))
 async def test_http_success_errors_redirect_and_rate_gate(self):
  from aiohttp import web
  mode=200;hits=[]
  async def handler(req):
   hits.append(req.headers.get('Authorization'))
   if mode==200:return web.json_response({'five_hour':{'utilization':40}})
   return web.Response(status=mode,headers={'Retry-After':'600'} if mode==429 else {})
  app=web.Application();app.router.add_get('/usage',handler)
  runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
  port=site._server.sockets[0].getsockname()[1]
  try:
   with patch.dict(g,USAGE_URL=f'http://127.0.0.1:{port}/usage',resolve_claude=lambda c:'claude',_run=AsyncMock(return_value=json.dumps({'loggedIn':True,'authMethod':'claude.ai'})),read_token=lambda:'test-token'):
    r=reader_type();self.assertIn('60%',(await r.fetch('claude',5)).text)
    with self.assertRaises(Exception):await r.fetch('claude',5)
    self.assertEqual(len(hits),1)
    for code in [401,403,302,500,429]:
     mode=code;r=reader_type()
     with self.assertRaises(Exception):await r.fetch('claude',5)
     if code==429:self.assertGreater(r.next_request-time.monotonic(),590)
  finally:await runner.cleanup()
 def test_missing_credentials_error_is_redacted(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(g['os'].environ,CLAUDE_CONFIG_DIR=tmp):
   with self.assertRaises(Exception) as e:g['read_token']()
   self.assertIn('auth login',str(e.exception));self.assertNotIn(tmp,str(e.exception))
 async def test_timeout_and_cancel_during_cli_check(self):
  entered=asyncio.Event();cancelled=asyncio.Event()
  async def hang(*args):
   entered.set()
   try:await asyncio.sleep(100)
   finally:cancelled.set()
  with patch.dict(g,resolve_claude=lambda c:'claude',_run=hang):
   with self.assertRaises(Exception) as err:await reader_type().fetch('claude',.02)
   self.assertIn('超时',str(err.exception));self.assertTrue(cancelled.is_set())
   entered.clear();cancelled.clear()
   task=asyncio.create_task(reader_type().fetch('claude',5));await entered.wait();task.cancel()
   with self.assertRaises(asyncio.CancelledError):await task
   self.assertTrue(cancelled.is_set())
 def test_expired_credentials_are_rejected_without_token_in_error(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(g['os'].environ,CLAUDE_CONFIG_DIR=tmp):
   Path(tmp,'.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'FAKE_SECRET_NOT_REAL','expiresAt':1}}),encoding='utf-8')
   with self.assertRaises(Exception) as e:g['read_token']()
   self.assertNotIn('FAKE_SECRET_NOT_REAL',str(e.exception));self.assertIn('auth login',str(e.exception))
 def test_survival_calendar_dates_and_invalid_input(self):
  from datetime import date
  text=Plugin._answer.__globals__['claude_survival_text']
  self.assertEqual(text('',date(2026,10,19)),'')
  self.assertIn('0天',text('2026-10-19',date(2026,10,19)))
  self.assertIn('18天',text('2026-10-01',date(2026,10,19)))
  self.assertIn('1天',text('2024-02-29',date(2024,3,1)))
  self.assertIn('还没到',text('2026-10-20',date(2026,10,19)))
  for value in ['bad','2026-02-30','2026-1-1']:
   self.assertIn('无效',text(value,date(2026,10,19)))
 async def test_survival_updates_without_refetching_quota(self):
  p=Plugin(Context(),{'claude_query_enabled':True,'claude_start_date':'2026-10-01'})
  try:
   p.claude_reader.fetch=AsyncMock(return_value=quota.QuotaReport('Claude quota'))
   with patch.dict(Plugin._answer.__globals__,claude_survival_text=lambda value:'days '+value):
    self.assertIn('days 2026-10-01',await p._answer(Event(),'claude'))
    p.config['claude_start_date']='2026-10-02'
    self.assertIn('days 2026-10-02',await p._answer(Event(),'claude'))
   self.assertEqual(p.claude_reader.fetch.await_count,1)
   p.config['claude_query_enabled']=False
   self.assertNotIn('days',await p._answer(Event(),'claude'))
  finally:await p.terminate()
if __name__=='__main__':unittest.main()
