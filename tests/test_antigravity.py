import sys,json,asyncio,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_quota import Plugin,Context,Event,quota
fetch=Plugin._antigravity_snapshot.__globals__['fetch_antigravity']
mod=sys.modules.get(fetch.__module__)
if mod is None:
 import importlib
 mod=importlib.import_module(fetch.__module__)
class Tests(unittest.IsolatedAsyncioTestCase):
 def test_windows_default_path_when_environment_is_redirected(self):
  if mod.os.name != 'nt':self.skipTest('Windows path fallback')
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp);exe=home/'AppData/Local/agy/bin/agy.exe'
   exe.parent.mkdir(parents=True);exe.write_bytes(b'fixture')
   with patch.object(mod.shutil,'which',return_value=None),patch.object(mod.Path,'home',return_value=home),patch.dict(mod.os.environ,LOCALAPPDATA=str(home/'redirected')):
    self.assertEqual(mod.resolve_agy('agy'),str(exe.resolve()))
    self.assertEqual(mod.resolve_agy('agy.exe'),str(exe.resolve()))
    self.assertEqual(mod.resolve_agy('"'+str(exe)+'"'),str(exe.resolve()))
    with self.assertRaises(Exception):mod.resolve_agy(str(home/'missing.exe'))
 async def test_default_off(self):
  p=Plugin(Context(),{'codex_query_enabled':False,'deepseek_query_enabled':False})
  try:
   with patch.dict(Plugin._antigravity_snapshot.__globals__,fetch_antigravity=AsyncMock(side_effect=AssertionError())):
    self.assertIn('关闭',await p._answer(Event(),'antigravity'))
    self.assertNotIn('Antigravity',await p._answer(Event(),'all'))
  finally: await p.terminate()
 async def test_enabled_and_mtk(self):
  p=Plugin(Context(),{'codex_query_enabled':False,'deepseek_query_enabled':False,'antigravity_query_enabled':True})
  try:
   with patch.dict(Plugin._antigravity_snapshot.__globals__,fetch_antigravity=AsyncMock(return_value=quota.QuotaReport('remaining 50%'))):
    e=Event(message='mtk',wake=False)
    await p.mtk_shortcut(e)
    self.assertIn('50%',e.sent[0]); self.assertTrue(e.stopped)
    e=Event(message='mtk',wake=False,admin=False)
    await p.mtk_shortcut(e)
    self.assertIn('管理员',e.sent[0])
    for msg in ['mtk x','hello mtk','mtk额度']:
     e=Event(message=msg,wake=False);await p.mtk_shortcut(e);self.assertEqual(e.sent,[])
  finally: await p.terminate()
 async def test_process_bounds_timeout_and_cancellation(self):
  with self.assertRaises(Exception):
   await mod._run(sys.executable,['-c',"import sys;sys.stdout.write('x'*1100000)"])
  with self.assertRaises(TimeoutError):
   async with asyncio.timeout(.2):
    await mod._run(sys.executable,['-c','import time;time.sleep(20)'])
  task=asyncio.create_task(mod._run(sys.executable,['-c','import time;time.sleep(20)']))
  await asyncio.sleep(.2);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
 async def test_version_guard(self):
  with patch.object(mod,'resolve_agy',return_value='agy'),patch.object(mod,'_run',AsyncMock(return_value='1.1.10')) as run:
   with self.assertRaises(Exception):await mod.fetch_antigravity('agy',2)
   self.assertEqual(run.await_count,1)
 def test_format(self):
  d={'command':{'name':'usage','data':{'groups':[{'name':'Gemini','buckets':[{'window':'weekly','remaining_fraction':.5,'reset_time':'2026-10-20T00:00:00Z'}]}]}}}
  self.assertIn('50%',mod.antigravity_report(d).text)
  for v in [True,-1,2,'0.5']:
   d['command']['data']['groups'][0]['buckets'][0]['remaining_fraction']=v
   with self.assertRaises(Exception):mod.antigravity_report(d)
 def test_natural(self):
  for s in ['帮我查一下反重力额度','agy还剩多少','查看antigravity额度']:
   self.assertEqual(quota.natural_target(s),'antigravity')
if __name__=='__main__':unittest.main()
