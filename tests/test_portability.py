"""Native process containment, event protocol, migration and installation tests."""
import io,json,os,queue,shutil,sqlite3,subprocess,sys,tempfile,time,unittest
from pathlib import Path,PureWindowsPath,PurePosixPath
from contextlib import ExitStack,redirect_stdout,closing
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
import compat,maintenance,runner,media_migration,store,app,model

class Portable(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='YT 日本語 ');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.state=self.root/'.state';self.state.mkdir()
        stack=ExitStack();self.addCleanup(stack.close)
        for module in (store,runner):stack.enter_context(patch.object(module,'STATE',self.state))
        stack.enter_context(patch.object(runner,'BASE',self.root))
    def worker(self,body):
        (self.root/'worker.py').write_text('import sys,os,json,time,subprocess\nsys.path.insert(0,'+repr(str(BASE))+')\nfrom compat import worker_start\nworker_start()\n'+body,encoding='utf-8')
    def test_corrupt_protocol_and_missing_final(self):
        for body in ('print("broken",flush=True)','print("[]",flush=True)','print(json.dumps({"type":"unexpected"}),flush=True)','print(json.dumps({"type":"progress","downloaded":"bad"}),flush=True)','sys.stdout.write("{\\\"type\\\":\\\"done\\\"}");sys.stdout.flush()','print(json.dumps({"type":"start"}),flush=True)','print(json.dumps({"type":"done"}),flush=True);print(json.dumps({"type":"done"}),flush=True)'):
            with self.subTest(body=body):
                self.worker(body)
                with self.assertRaises(runner.WorkerError):runner.run({})
        self.assertFalse(list(self.state.glob('worker-*.json')))
    def test_maximum_event_size(self):
        self.worker('sys.stdout.write("x"*(32*1024**2+1));sys.stdout.flush()')
        with self.assertRaisesRegex(runner.WorkerError,'besar'):runner.run({})
    def test_worker_waits_for_start(self):
        self.worker('(Path if False else open)(sys.argv[1]+".started","w").close()')
        p=subprocess.Popen([sys.executable,str(self.root/'worker.py'),str(self.root/'request')],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            time.sleep(.2);self.assertFalse((self.root/'request.started').exists());p.stdin.write(b'START\n');p.stdin.close();p.wait(timeout=5);self.assertTrue((self.root/'request.started').exists())
        finally:
            if p.poll() is None:p.kill();p.wait()
    def test_cross_process_lock_and_release(self):
        code='import sys,time;sys.path.insert(0,sys.argv[1]);from compat import file_lock\nwith file_lock(sys.argv[2]):print("ready",flush=True);sys.stdin.readline()'
        p=subprocess.Popen([sys.executable,'-c',code,str(BASE),str(self.state/'run.lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        try:
            self.assertEqual(p.stdout.readline().strip(),b'ready')
            with self.assertRaises(ValueError):
                with compat.file_lock(self.state/'run.lock'):pass
            p.stdin.write(b'END\n');p.stdin.close();p.wait(timeout=5)
            with compat.file_lock(self.state/'run.lock'):pass
        finally:
            if p.poll() is None:p.kill();p.wait()
            p.stdout.close()
    def alive(self,pid):
        if os.name=='nt':
            import ctypes
            k=ctypes.WinDLL('kernel32');k.OpenProcess.restype=ctypes.c_void_p;k.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];k.CloseHandle.argtypes=[ctypes.c_void_p];k.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
            h=k.OpenProcess(0x1000,False,pid)
            if not h:return False
            value=ctypes.c_ulong();k.GetExitCodeProcess(h,ctypes.byref(value));k.CloseHandle(h);return value.value==259
        try:
            os.kill(pid,0)
            if sys.platform.startswith('linux') and ') Z' in Path(f'/proc/{pid}/stat').read_text():return False
            r=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
            return bool(r.stdout.strip()) and not r.stdout.strip().startswith('Z')
        except (ProcessLookupError,FileNotFoundError):return False
    def test_crash_and_interrupt_kill_ffmpeg_descendant(self):
        # Real FFmpeg processes emulate an in-flight download/merge child.
        for crash in (False,True):
            self.worker('p=subprocess.Popen(["ffmpeg","-v","error","-re","-f","lavfi","-i","testsrc2=size=32x32:rate=1","-f","null","-"],stdout=sys.stderr,stderr=sys.stderr)\nopen(os.path.join(os.path.dirname(sys.argv[1]),"child.pid"),"w").write(str(p.pid))\n'+('os._exit(7)' if crash else 'print(json.dumps({"type":"stage","message":"merge"}),flush=True)\ntime.sleep(60)'))
            def interrupt(event):raise KeyboardInterrupt()
            with self.assertRaises(runner.WorkerError if crash else KeyboardInterrupt):runner.run({},interrupt)
            pid=int((self.state/'child.pid').read_text());deadline=time.monotonic()+5
            while self.alive(pid) and time.monotonic()<deadline:time.sleep(.1)
            self.assertFalse(self.alive(pid),'FFmpeg left running')
    def test_filename_unicode_and_windows_reserved_characters(self):
        item={'title':'CON<>:/\\|?* 日本語😀'+('a'*400),'id':'x','choice':{'mode':'audio','actual_quality':None,'audio_format':'original'},'subtitles':[]}
        name=model.basename(item);self.assertLess(len(name.encode('utf-8')),220)
        for c in '<>:/\\|?*':self.assertNotIn(c,name)
        self.assertIn('日本語',name)
    def prepare_rebase(self,old):
        self.new=self.root/'new media 日本語';(self.new/'video').mkdir(parents=True);(self.new/'video/a.mp4').write_bytes(b'media')
        source=str(media_migration.source_path(old)/'video/a.mp4')
        job={'id':'abc','output':old,'browser':'firefox:old-profile','items':[{'result':{'path':source}}]};store.write_json(self.state/'jobs/abc.json',job);store.write_json(self.root/'settings.json',{'output':old,'browser':'firefox:old'})
        db=sqlite3.connect(self.state/'downloaded.sqlite3');db.execute('CREATE TABLE results (variant TEXT,path TEXT,payload TEXT,PRIMARY KEY(variant,path))');db.execute('INSERT INTO results VALUES (?,?,?)',('v',source,json.dumps({'path':source,'mode':'video','subtitles':[]})));db.commit();db.close()
    def test_foreign_rebase_dry_run_idempotence_and_browser_reset(self):
        old=r'C:\Users\old\Videos\YouTube' if os.name!='nt' else '/home/old/Videos/YouTube';self.prepare_rebase(old)
        before=(self.state/'jobs/abc.json').read_bytes()
        with redirect_stdout(io.StringIO()):
            preview=media_migration.rebase_media(self.root,old,str(self.new),True);self.assertGreater(preview['changed'],0);self.assertEqual(before,(self.state/'jobs/abc.json').read_bytes())
            media_migration.rebase_media(self.root,old,str(self.new));again=media_migration.rebase_media(self.root,old,str(self.new),True)
        self.assertEqual(again['changed'],0);job=store.load_job('abc');self.assertEqual(job['output'],str(self.new));self.assertIsNone(job['browser']);self.assertTrue(list((self.state/'media-path-backups').iterdir()))
        with closing(sqlite3.connect(self.state/'downloaded.sqlite3')) as db:self.assertEqual(db.execute('SELECT path FROM results').fetchone()[0],str(self.new/'video/a.mp4'))
    def test_rebase_missing_file_preserves_records(self):
        old='/old/YouTube';self.prepare_rebase(old);(self.new/'video/a.mp4').unlink();before=(self.state/'downloaded.sqlite3').read_bytes()
        with redirect_stdout(io.StringIO()),self.assertRaisesRegex(ValueError,'belum lengkap'):media_migration.rebase_media(self.root,old,str(self.new))
        self.assertEqual(before,(self.state/'downloaded.sqlite3').read_bytes())
    def test_rebase_failure_restores_job_and_database(self):
        old='/old/YouTube';self.prepare_rebase(old);job=(self.state/'jobs/abc.json').read_bytes();db=(self.state/'downloaded.sqlite3').read_bytes()
        with patch.object(store,'write_json',side_effect=OSError('disk failure')),redirect_stdout(io.StringIO()),self.assertRaises(OSError):media_migration.rebase_media(self.root,old,str(self.new))
        self.assertEqual(job,(self.state/'jobs/abc.json').read_bytes());self.assertEqual(db,(self.state/'downloaded.sqlite3').read_bytes())
    def test_foreign_output_refused_and_linux_rollback_restricted(self):
        foreign=r'C:\Users\old\Videos' if os.name!='nt' else '/home/old/Videos'
        with self.assertRaisesRegex(ValueError,'rebase-media'):compat.native_output(foreign)
        backup=self.root/'backup';backup.mkdir();(backup/'manifest.json').write_text(json.dumps({'files':[],'added':[]}))
        with patch.object(maintenance,'platform_key',return_value='windows-x86_64'),self.assertRaisesRegex(ValueError,'ditolak'):maintenance.restore(self.root,backup)

class NativeInstaller(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('YT_TEST_INSTALL')=='1','CI enables clean dependency installation')
    def test_clean_install_unicode_update_migrate_rollback(self):
        with tempfile.TemporaryDirectory(prefix='YT setup 日本語 ') as folder:
            root=Path(folder)/'fresh tool';root.mkdir()
            for name in maintenance.program_files(BASE):
                dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(BASE/name,dest)
            # Installer must choose its own verified OS asset, never reuse another OS runtime.
            for args in (['setup'],['setup','--check']):subprocess.run([sys.executable,str(root/'maintenance.py'),*args],check=True,timeout=300)
            py=compat.python_path(root);subprocess.run([str(py),str(root/'app.py'),'preview','--plain'],check=True,timeout=15)
            moved=Path(folder)/'moved tools';subprocess.run([sys.executable,str(root/'maintenance.py'),'migrate',str(moved)],check=True,timeout=300)
            subprocess.run([sys.executable,str(root/'maintenance.py'),'update',str(moved)],check=True,timeout=300)
            subprocess.run([sys.executable,str(moved/'maintenance.py'),'rollback'],check=True,timeout=300)
            subprocess.run([sys.executable,str(moved/'maintenance.py'),'setup','--check'],check=True,timeout=60)
