"""Stable-release protocol, package validation and real isolated installation."""
import hashlib,io,json,os,shutil,subprocess,sys,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
import compat,maintenance,release_bundle,self_update as update
from terminal import Terminal

class SelfUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'new';self.source.mkdir();self.dest=self.root/'installed';self.dest.mkdir()
        (self.dest/'model.py').write_text("VERSION='1.1.0'\n",encoding='utf-8')
        (self.dest/'app.py').write_text('old program',encoding='utf-8')
        (self.dest/'settings.json').write_text('{"private":true}',encoding='utf-8')
        for name in ('maintenance.py','compat.py','app.py','requirements.lock','runtime.json','youtube-downloader','youtube-downloader.cmd'):
            (self.source/name).write_text('# fixture\n',encoding='utf-8')
        (self.source/'model.py').write_text("VERSION='1.2.0'\n",encoding='utf-8')
        meta={'version':'1.2.0','platforms':[compat.platform_key()],'python_min':'3.10','python_max':'3.14'}
        (self.source/'release.json').write_text(json.dumps(meta),encoding='utf-8')
        self.archive,self.checksum=release_bundle.build(self.source,self.root/'packages')
        self.info={'tag_name':'v1.2.0','draft':False,'prerelease':False,'assets':[{'id':1,'name':self.archive.name,'state':'uploaded','size':self.archive.stat().st_size},{'id':2,'name':self.checksum.name,'state':'uploaded','size':self.checksum.stat().st_size}]}
        self.data={'/releases/latest':json.dumps(self.info).encode(),'/releases/assets/1':self.archive.read_bytes(),'/releases/assets/2':self.checksum.read_bytes()}
        class Client:
            def read(inner,path,limit,asset=False):return self.data[path]
        self.client=Client()
    def perform(self,**kw):
        with patch.object(update,'github_token',return_value='test-credential'),patch.object(update,'GitHub',return_value=self.client):
            return update.perform(self.dest,say=lambda msg:None,**kw)
    def test_check_is_read_only(self):
        self.assertEqual(self.perform(check=True),0);self.assertFalse((self.dest/'.state').exists())
    def test_equal_or_older_release_is_not_installed(self):
        for version in ('1.2.0','1.3.0'):
            (self.dest/'model.py').write_text("VERSION='"+version+"'",encoding='utf-8')
            with patch.object(update,'install_package') as install:self.assertEqual(self.perform(),0);install.assert_not_called()
        self.assertFalse((self.dest/'.state').exists())
    def test_package_verified_before_install_and_temporary_cleaned(self):
        def install(source,root,*args):
            self.assertEqual(update.installed_version(source),'1.2.0')
            shutil.copy2(source/'model.py',root/'model.py');return 0
        with patch.object(update,'install_package',side_effect=install):self.assertEqual(self.perform(),0)
        self.assertEqual((self.dest/'settings.json').read_text(encoding='utf-8'),'{"private":true}')
        self.assertEqual(list((self.dest/'.state').glob('release-*')),[])
    def test_shared_download_lock_refuses_installation(self):
        with compat.file_lock(self.dest/'.state/run.lock'),patch.object(update,'install_package') as install:
            with self.assertRaisesRegex(ValueError,'berjalan'):self.perform()
            install.assert_not_called()
        self.assertEqual(update.installed_version(self.dest),'1.1.0')
    def test_bad_checksum_leaves_program_unchanged(self):
        self.data['/releases/assets/1']=b'x'*self.info['assets'][0]['size']
        with patch.object(update,'install_package') as install:
            with self.assertRaisesRegex(ValueError,'Checksum'):self.perform()
            install.assert_not_called()
        self.assertEqual(update.installed_version(self.dest),'1.1.0');self.assertFalse(list((self.dest/'.state').glob('release-*')))
    def test_checksum_filename_mismatch_rejected(self):
        self.data['/releases/assets/2']=b'0'*64+b'  other.zip\n'
        with self.assertRaisesRegex(ValueError,'checksum'):self.perform()
    def test_unstable_invalid_and_incomplete_release_rejected(self):
        for change in ({'prerelease':True},{'draft':True},{'tag_name':'v1.2.0-rc1'},{'assets':[]}):
            self.data['/releases/latest']=json.dumps({**self.info,**change}).encode()
            with self.assertRaises(ValueError):update.release_info(self.client)
    def test_zip_traversal_symlink_private_and_duplicate_rejected(self):
        for name in ('../escape','youtube-downloader/../escape','youtube-downloader/settings.json','youtube-downloader/.state/archive','youtube-downloader/tests/../../escape','youtube-downloader/C:\\escape'):
            archive=self.root/'invalid.zip'
            with zipfile.ZipFile(archive,'w') as out:out.writestr(name,b'evil')
            with self.assertRaises(ValueError):update.safe_unpack(archive,self.root/'extract','1.2.0')
        for kind in ('symlink','duplicate'):
            with zipfile.ZipFile(self.root/'invalid.zip','w') as out:
                member=zipfile.ZipInfo('youtube-downloader/app.py');member.external_attr=0o120777<<16 if kind=='symlink' else 0o100644<<16
                out.writestr(member,b'target')
                if kind=='duplicate':out.writestr('youtube-downloader/APP.PY',b'duplicate')
            with self.assertRaises(ValueError):update.safe_unpack(self.root/'invalid.zip',self.root/('extract-'+kind),'1.2.0')
    def test_package_version_platform_and_python_checked(self):
        meta=json.loads((self.source/'release.json').read_text())
        for change in ({'version':'1.9.0'},{'platforms':['unavailable']},{'python_min':'9.0','python_max':'9.1'}):
            (self.source/'release.json').write_text(json.dumps({**meta,**change}),encoding='utf-8')
            archive,_=release_bundle.build(self.source,self.root/'packages')
            with self.assertRaises(ValueError):update.safe_unpack(archive,self.root/('extract-'+str(len(list(self.root.glob('extract-*'))))),'1.2.0')
    def test_redirect_removes_credentials_and_rejects_other_hosts(self):
        handler=update.SafeRedirect();request=update.urllib.request.Request('https://api.github.com/a',headers={'Authorization':'Bearer secret'})
        redirected=handler.redirect_request(request,None,302,'',{},'https://release-assets.githubusercontent.com/a')
        self.assertFalse(redirected.has_header('Authorization'))
        for url in ('http://github.com/a','https://evil.example/a','https://githubusercontent.com.evil.example/a'):
            with self.assertRaises(ValueError):handler.redirect_request(request,None,302,'',{},url)
    def test_credentials_use_environment_without_spawning_or_saving(self):
        with patch.dict(os.environ,{'GH_TOKEN':'test-secret'}),patch.object(update.subprocess,'run') as run:
            self.assertEqual(update.github_token(),'test-secret');run.assert_not_called()
    def test_release_bundle_excludes_private_data_and_has_executable_launcher(self):
        for name in ('settings.json','.state/private','.venv/private','.runtime/private','media/private'):
            file=self.source/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('private')
        archive,_=release_bundle.build(self.source,self.root/'packages')
        with zipfile.ZipFile(archive) as source:
            self.assertFalse(any('private' in x or 'settings.json' in x for x in source.namelist()))
            self.assertTrue(source.getinfo('youtube-downloader/youtube-downloader').external_attr>>16&0o111)
    def test_real_isolated_installer_uses_new_package_and_preserves_data(self):
        (self.source/'compat.py').write_text((BASE/'compat.py').read_text(encoding='utf-8'),encoding='utf-8')
        # Use the real transactional updater with dependency setup replaced by a local fixture.
        script=(BASE/'maintenance.py').read_text(encoding='utf-8').replace("if __name__=='__main__':sys.exit(main())","setup=lambda *args,**kwargs:None\nif __name__=='__main__':sys.exit(main())")
        (self.source/'maintenance.py').write_text(script,encoding='utf-8');(self.dest/'.state').mkdir()
        (self.dest/'.state/archive').write_bytes(b'keep database');(self.dest/'media').mkdir();(self.dest/'media/result').write_bytes(b'keep media')
        self.assertEqual(update.install_package(self.source,self.dest,say=lambda msg:None),0)
        self.assertEqual(update.installed_version(self.dest),'1.2.0')
        self.assertEqual((self.dest/'.state/archive').read_bytes(),b'keep database');self.assertEqual((self.dest/'media/result').read_bytes(),b'keep media')
        self.assertTrue((self.dest/'.state/last-update.json').exists())
    def test_real_installer_failure_restores_program(self):
        (self.source/'compat.py').write_text((BASE/'compat.py').read_text(encoding='utf-8'),encoding='utf-8')
        script=(BASE/'maintenance.py').read_text(encoding='utf-8').replace("if __name__=='__main__':sys.exit(main())","def setup(root,**kw):\n    if not kw.get('upgrade'):raise ValueError('fixture failure')\nif __name__=='__main__':sys.exit(main())")
        (self.source/'maintenance.py').write_text(script,encoding='utf-8');(self.dest/'.state').mkdir()
        with self.assertRaisesRegex(ValueError,'tidak berhasil'):update.install_package(self.source,self.dest,say=lambda msg:None)
        self.assertEqual(update.installed_version(self.dest),'1.1.0');self.assertEqual((self.dest/'app.py').read_text(encoding='utf-8'),'old program')
    def test_ctrl_c_during_install_is_deferred_and_returns_130(self):
        (self.source/'maintenance.py').write_text("import time,sys\nfrom pathlib import Path\np=Path(sys.argv[2]);(p/'.state/started').write_text('started');time.sleep(1.2);(p/'.state/finished').write_text('finished')\n",encoding='utf-8')
        (self.dest/'.state').mkdir()
        script='from pathlib import Path; from self_update import install_package; import sys; sys.exit(install_package(Path(sys.argv[1]),Path(sys.argv[2])))'
        with Terminal([sys.executable,'-c',script,str(self.source),str(self.dest)],BASE) as term:
            deadline=time.monotonic()+10
            while not (self.dest/'.state/started').exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue((self.dest/'.state/started').exists());term.interrupt();data=''
            while term.running() and time.monotonic()<deadline:data+=term.read(.1)
            data+=term.drain();self.assertEqual(term.wait(),130,data)
        self.assertTrue((self.dest/'.state/finished').exists())

if __name__=='__main__':unittest.main()
