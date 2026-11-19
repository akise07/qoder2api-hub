"""Conservative central logging regression fixtures."""
import os,sys,tempfile,json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *
sys.path.insert(0, ROOT)
from unittest import mock
from pathlib import Path
import qoder_logpaths as L
def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))
declare_suite(__file__,['qoder_logpaths.py','start-qoder-proxy.bat','start-qoder-proxy.sh'])
with tempfile.TemporaryDirectory() as d:
    with mock.patch.dict(os.environ,{},clear=True):
        check('default logs path',canonical(L.resolve_log_root(d))==canonical(os.path.join(d,'logs')))
        check('default usage nested in logs',canonical(L.resolve_usage_dir(d))==canonical(os.path.join(d,'logs','usage')))
        os.mkdir(os.path.join(d,'usage'))
        src=os.path.join(d,'usage','usage.jsonl')
        with open(src,'w') as f:f.write('history\n')
        r=L.migrate_legacy_usage(d)
        check('legacy copied before first new start',r['copied']==['usage.jsonl'])
        check('source never deleted',os.path.isfile(src))
        dst=os.path.join(L.resolve_usage_dir(d),'usage.jsonl')
        with open(dst,'a') as f:f.write('new\n')
        L.migrate_legacy_usage(d)
        with open(dst) as f:check('existing history never overwritten',f.read()=='history\nnew\n')
        with mock.patch.dict(os.environ,{'QD_LOG_DIR':os.path.join(d,'custom')}):
            check('custom log root',canonical(L.resolve_log_root(d))==canonical(os.path.join(d,'custom')))
        with mock.patch.dict(os.environ,{'QD_PROXY_USAGE_DIR':os.path.join(d,'explicit')}):
            check('explicit usage remains authoritative',canonical(L.resolve_usage_dir(d))==canonical(os.path.join(d,'explicit')))
with open(os.path.join(ROOT,'start-qoder-proxy.bat'),'rb') as f:
    content=f.read()
check('bat stays ASCII',all(x<128 for x in content))
check('launcher logs in shared folder',b'QD_LOG_DIR' in content and b'gateway.err.log' in content)
with tempfile.TemporaryDirectory() as d:
    with mock.patch.dict(os.environ,{'QD_LOG_DIR':d},clear=False):
        import io,threading
        tee=L._LogTee(io.StringIO(),os.path.join(d,'tee.log'))
        tee.write('bad \udce4 char\n')
        tee.file.close()
        tee.write('after close\n')
        check('tee never raises (unencodable text / closed log file)',True)
        old_hooks=(threading.excepthook,sys.unraisablehook)
        crash=L.install_crash_logging(d)
        t=threading.Thread(target=lambda:(_ for _ in ()).throw(ValueError('crash probe')),name='probe')
        t.start();t.join()
        threading.excepthook,sys.unraisablehook=old_hooks
        body=open(crash,encoding='utf-8').read() if os.path.isfile(crash) else ''
        check('thread crash leaves one readable record',body.count('[thread-crash]')==1)
        check('thread crash keeps the traceback', 'ValueError: crash probe' in body)
finish()
