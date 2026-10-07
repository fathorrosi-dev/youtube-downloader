"""Offline regression tests: real yt-dlp, FFmpeg and local HTTP media."""
import copy,io,json,os,signal,sqlite3,subprocess,sys,tempfile,threading,time,unittest
from contextlib import ExitStack,redirect_stderr
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
import model,store,runner,app,ui,maintenance
from rich.console import Console
from terminal import Terminal

def synthetic():
    def v(identifier,w,h,fps=30,codec='avc1'):return {'format_id':identifier,'width':w,'height':h,'fps':fps,'vcodec':codec,'acodec':'none'}
    return {'id':'x','title':'Example','formats':[
        {'format_id':'dub','acodec':'mp4a','vcodec':'none','language':'ja','language_preference':-10},
        {'format_id':'a1','acodec':'mp4a','vcodec':'none','language':'en','language_preference':10,'abr':128},
        {'format_id':'a2','acodec':'opus','vcodec':'none','language':'en','language_preference':10,'abr':160},
        v('720',1280,720),v('1080',1920,1080),v('1080b',1920,1080,60,'vp9'),v('1440',2560,1440),v('2160',3840,2160),v('4320',7680,4320)]}

class Selection(unittest.TestCase):
    def test_quality_pairs_and_best(self):
        info=synthetic()
        for q,identifier in ((720,'720'),(1080,'1080b'),(1440,'1440'),(2160,'2160'),('best','4320')):
            c=model.pick(info,quality=q);self.assertEqual(c['video_format'],identifier);self.assertEqual(c['audio_format_id'],'a2');self.assertEqual(c['language'],'en')
    def test_portrait_fallback_and_no_upscale(self):
        info=synthetic()
        for f in info['formats']:
            if f.get('width'):f['width'],f['height']=f['height'],f['width']
        self.assertEqual(model.qualities(info),[4320,2160,1440,1080,720])
        self.assertEqual(model.pick(info,quality=1200)['actual_quality'],1080)
        with self.assertRaises(model.SelectionError):model.pick(info,quality=480)
    def test_modes_languages_and_containers(self):
        info=synthetic();c=model.pick(info,mode='video',quality=1080)
        self.assertIsNone(c['audio_format_id']);self.assertEqual(model.container(c,[]),'mkv')
        c=model.pick(info,mode='audio',language='ja');self.assertIsNone(c['video_format']);self.assertEqual(c['audio_format_id'],'dub')
        self.assertEqual(model.container(c,[]),'m4a')
        c=model.pick(info,quality=720,audio_id='a1');self.assertEqual(model.container(c,[]),'mp4');self.assertEqual(model.container(c,[{}]),'mkv')
    def test_tracks_and_variants(self):
        info={'subtitles':{'en':[{'url':'https://x/sub?lang=en','name':'English','ext':'vtt'}]},'automatic_captions':{'ja-en':[{'url':'https://x/sub?lang=en&tlang=ja','name':'Japanese from English','ext':'vtt'}],'en-orig':[{'url':'https://x/sub?lang=en','name':'English','ext':'vtt'}],'en':[{'url':'https://x/sub?lang=en','name':'English','ext':'vtt'}]}}
        ts=model.tracks(info);self.assertEqual(len(ts),3);self.assertEqual(ts[1]['kind'],'translated');self.assertEqual(ts[1]['source'],'en')
        item={'id':'x','title':'a/b','choice':model.pick(synthetic(),quality=1080),'subtitles':[]}
        key=model.variant(item);other=copy.deepcopy(item);other['choice']['estimated_size']=999;self.assertEqual(key,model.variant(other))
        other['choice']['quality']=1200;self.assertEqual(key,model.variant(other))
        other['choice']=model.pick(synthetic(),quality=1440);self.assertNotEqual(key,model.variant(other));self.assertNotIn('/',model.basename(item))
    def test_indices_and_error_classification(self):
        self.assertEqual(model.item_indices('1,3,5-7',7),[0,2,4,5,6])
        for bad in ('0','8','3-1','x'):
            with self.assertRaises(model.SelectionError):model.item_indices(bad,7)
        self.assertEqual(model.classify('Sign in to confirm your age'),'authentication');self.assertEqual(model.classify('HTTP 429'),'rate_limit')
        self.assertEqual(model.classify('Private video'),'unavailable')
        with self.assertRaises(model.SelectionError):model.valid_url('https://evil.example/video')

class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory();cls.root=Path(cls.folder.name);web=cls.root/'web';web.mkdir()
        def ff(*args):subprocess.run(['ffmpeg','-v','error','-y',*args],check=True)
        ff('-f','lavfi','-i','testsrc2=size=160x90:rate=10','-t','1','-c:v','libx264','-threads','1','-preset','ultrafast','-an',str(web/'v.mp4'))
        ff('-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','1','-c:a','aac',str(web/'a.m4a'))
        (web/'en.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nHello\n')
        (web/'ja.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nこんにちは\n',encoding='utf-8')
        class Handler(SimpleHTTPRequestHandler):
            flaky=0
            def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(web),**kwargs)
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.host='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.folder.cleanup()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.state=self.root/'.state';self.state.mkdir();self.media=self.root/'media'
        fixture={'duration':1,'channel':'Local','formats':[
            {'format_id':'a','url':self.host+'/a.m4a','acodec':'mp4a.40.2','vcodec':'none','ext':'m4a','language':'en','language_preference':10},
            {'format_id':'v','url':self.host+'/v.mp4','acodec':'none','vcodec':'avc1','ext':'mp4','width':160,'height':90,'fps':10}],
            'subtitles':{'en':[{'url':self.host+'/en.vtt?lang=en','ext':'vtt','name':'English'}]},
            'automatic_captions':{'ja-en':[{'url':self.host+'/ja.vtt?lang=en&tlang=ja','ext':'vtt','name':'Japanese from English'}]}}
        (self.root/'fixture.json').write_text(json.dumps(fixture))
        (self.root/'worker.py').write_text('import sys,runpy\nsys.argv.insert(1,'+repr(str(BASE))+')\nrunpy.run_path('+repr(str(BASE/'tests/fixture_worker.py'))+',run_name="__main__")\n')
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for module in (runner,store,app):self.stack.enter_context(patch.object(module,'STATE',self.state))
        self.stack.enter_context(patch.object(runner,'BASE',self.root))
    def inspect(self):return runner.run({'action':'inspect','url':'fixture:sample'})
    def item(self,mode='video-audio',subs=False,audio_format='original'):
        info=self.inspect();return {'id':'sample','title':'Local fixture','url':'fixture:sample','choice':model.pick(info,mode,90,audio_format=audio_format),'subtitles':model.tracks(info) if subs else [],'status':'pending'}
    def download(self,item,**extras):
        events=[];result=runner.run({'action':'download','item':item,'output':str(self.media),**extras},events.append)
        return result,events
    def test_real_merge_video_only_audio_and_mp3(self):
        for mode,audio_format in (('video-audio','original'),('video','original'),('audio','original'),('audio','mp3')):
            item=self.item(mode,audio_format=audio_format);result,events=self.download(item)
            probe=store.probe(result['path'],mode);self.assertTrue(Path(result['path']).is_file())
            self.assertTrue(any(x['type']=='progress' for x in events));self.assertTrue(any(x['type']=='stage' for x in events))
            if mode=='video-audio':self.assertEqual(Path(result['path']).suffix,'.mp4')
            if mode!='audio':self.assertEqual(next(s['codec_name'] for s in probe['streams'] if s['codec_type']=='video'),'h264')
            self.assertFalse((self.root/'.state/temporary'/model.variant(item)).exists())
    def test_subtitles_embedding_and_retry_without_redownload(self):
        item=self.item(subs=True);item['subtitles'].append({'key':'zz','origin':'subtitles','language':'zz','kind':'manual','name':'Missing','source':''})
        result,events=self.download(item);self.assertEqual(len(result['missing']),1);self.assertEqual(len(result['subtitles']),2)
        data=store.probe(result['path'],'video-audio');self.assertEqual(sum(s['codec_type']=='subtitle' for s in data['streams']),2)
        result2,events2=self.download(item,existing_file=result['path']);self.assertFalse(any(e['type']=='start' for e in events2));self.assertEqual(len(result2['missing']),1)
    def test_archive_skip_deleted_recovery_and_collision(self):
        item=self.item();result,_=self.download(item);key=model.variant(item)
        archive=store.Archive();self.addCleanup(archive.close);archive.put(key,result)
        self.assertIsNotNone(archive.get(key,'video-audio'))
        alternate=self.root/'alternate';alternate.mkdir();second=alternate/Path(result['path']).name
        import shutil
        shutil.copy2(result['path'],second);archive.put(key,{**result,'path':str(second)})
        self.assertEqual(archive.get(key,'video-audio',self.media)['path'],result['path'])
        self.assertEqual(archive.get(key,'video-audio',alternate)['path'],str(second))
        second.unlink()
        with self.assertRaises(runner.WorkerError):self.download(item)
        Path(result['path']).unlink();self.assertIsNone(archive.get(key,'video-audio'));result,_=self.download(item);archive.put(key,result)
        self.assertIsNotNone(archive.get(key,'video-audio'))
    def test_worker_crash_and_auth_errors(self):
        for name,category in (('auth','authentication'),('gone','unavailable'),('error','system'),('crash','system')):
            with self.assertRaises(runner.WorkerError) as caught:runner.run({'action':'inspect','url':'fixture:'+name})
            self.assertEqual(caught.exception.category,category)
    def test_batch_breaker_and_checkpoint(self):
        item=self.item();job=store.new_job([{**copy.deepcopy(item),'id':str(i)} for i in range(5)],str(self.media))
        with patch.object(app,'run',side_effect=runner.WorkerError('API structure changed','system')) as mock,redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):app.execute(ui.UI(True),job)
        self.assertEqual(mock.call_count,3);self.assertEqual(store.load_job(job['id'])['status'],'blocked')
    def test_interrupt_checkpoint_and_lock(self):
        item=self.item();job=store.new_job([item],str(self.media))
        with patch.object(app,'run',side_effect=KeyboardInterrupt),redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):app.execute(ui.UI(True),job)
        self.assertEqual(store.load_job(job['id'])['status'],'interrupted')
        with store.lock():
            with self.assertRaises(ValueError):
                with store.lock():pass

class Display(unittest.TestCase):
    def test_widths_colors_and_fraction(self):
        for width in (40,60,100,160):
            screen=ui.Session(ui.UI(True),10);screen.overall.update(screen.oid,completed=5)
            screen.event({'type':'start','name':'long '*100,'stream':'video'});screen.event({'type':'progress','downloaded':50*1024**2,'total':100*1024**2,'speed':1024**2,'eta':50})
            buffer=io.StringIO();Console(file=buffer,width=width,no_color=True).print(screen)
            lines=buffer.getvalue().splitlines();self.assertEqual(len(lines),7);self.assertTrue(all(len(x)<=width for x in lines));self.assertIn('50%',lines[0]);self.assertIn('█',lines[0])
        self.assertEqual(ui.SolidBar(50,100,ui.PINK).text(20).plain,'█'*10+'░'*10)
    def tty(self,no_color=False,interrupt=False):
        with Terminal([sys.executable,str(BASE/'app.py'),'preview'],BASE,no_color=no_color) as term:
            result='';sent=False;deadline=time.monotonic()+15
            while term.running() and time.monotonic()<deadline:
                result+=term.read(.1)
                if interrupt and not sent and '█' in result:term.interrupt();sent=True
            result+=term.drain()
            self.assertEqual(term.wait(),130 if interrupt else 0,result)
            return result
    def test_tty_plain_no_color_and_interrupt(self):
        out=self.tty()
        for rgb in ('168;216;240','255;194;212','232;56;79'):self.assertIn('\x1b[38;2;'+rgb+'m',out)
        self.assertRegex(out,r'\x1b\[[1-9][0-9]*A');self.assertIn('\x1b[?25h',out)
        out=self.tty(no_color=True);self.assertNotIn('\x1b[38;',out);self.assertIn('█',out)
        out=self.tty(interrupt=True);self.assertIn('Dihentikan',out);self.assertIn('\x1b[?25h',out)
        result=subprocess.run([sys.executable,str(BASE/'app.py'),'preview','--plain'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0);self.assertNotIn('█',result.stderr);self.assertLess(len(result.stderr.splitlines()),25)
    def test_animation_pause_and_reset(self):
        screen=ui.Session(ui.UI(True),1)
        screen.event({'type':'start','name':'x'});self.assertIsNotNone(screen.clock.resumed_at)
        for kind in ('wait','failure','stage','retry'):
            screen.event({'type':kind,'message':'test'});self.assertIsNone(screen.clock.resumed_at)
            screen.event({'type':'progress','downloaded':1});self.assertIsNotNone(screen.clock.resumed_at)
        screen.event({'type':'start','name':'y'});self.assertEqual(screen.downloaded,0);self.assertIsNone(screen.byte_total)

if __name__=='__main__':unittest.main(verbosity=2)
