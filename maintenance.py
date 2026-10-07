"""Local setup, release install, backup, rollback and package migration."""
import sysconfig
import argparse,base64,contextlib,hashlib,json,os,platform,re,shutil,subprocess,sys,tempfile,time,urllib.request,tarfile
from pathlib import Path
from compat import file_lock,python_path,deno_path,platform_key,environment_identity

BASE=Path(__file__).resolve().parent

def guard(root,wait=False):return file_lock(Path(root)/'.state/run.lock',wait)

def run_logged(root,args):
    with (root/'.state/setup.log').open('a',encoding='utf-8') as log:
        result=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PIP_CACHE_DIR':str(root/'.state/pip-cache')})
    if result.returncode:raise ValueError('Pemeliharaan gagal. Periksa '+str(root/'.state/setup.log'))

def runtime_asset(runtime):
    assets=runtime['assets'];key=platform_key()
    return assets.get(key) or (assets.get(platform.machine()) if key.startswith('linux-') else None)

def download_runtime(root):
    runtime=json.loads((root/'runtime.json').read_text(encoding='utf-8'));version=runtime['deno_version']
    binary=deno_path(root)
    installed=root/'.runtime/installed.json'
    if binary.exists() and installed.exists() and json.loads(installed.read_text(encoding='utf-8')).get('platform')==platform_key():
        try:
            result=subprocess.run([str(binary),'--version'],capture_output=True,text=True)
            if result.returncode==0 and result.stdout.startswith('deno '+version+' '):return
        except OSError:pass
    if binary.exists() and not installed.exists() and platform_key().startswith('linux-') and binary.read_bytes()[:4]==b'\x7fELF' and int.from_bytes(binary.read_bytes()[18:20],'little')==({'linux-x86_64':62,'linux-aarch64':183}.get(platform_key())):
        try:
            r=subprocess.run([str(binary),'--version'],capture_output=True,text=True,timeout=10)
            if r.returncode==0 and r.stdout.startswith('deno '+version+' '):
                installed.write_text(json.dumps({'platform':platform_key(),'version':version}),encoding='utf-8');return
        except OSError:pass
    asset=runtime_asset(runtime)
    if not asset:raise ValueError('Platform Deno belum didukung: '+platform_key())
    url=asset['url'];integrity=asset['integrity']
    if not url.startswith('https://registry.npmjs.org/@deno/') or not integrity.startswith('sha512-'):raise ValueError('Sumber runtime tidak valid')
    expected=base64.b64decode(integrity.split('-',1)[1],validate=True)
    if len(expected)!=64:raise ValueError('Checksum runtime tidak valid')
    binary.parent.mkdir(parents=True,exist_ok=True)
    archive=root/'.runtime'/('deno-'+version+'-'+platform_key()+'.tgz.part')
    print('Menyiapkan Deno lokal '+version+' · distribusi resmi npm',flush=True)
    for attempt in range(3):
        offset=archive.stat().st_size if archive.exists() else 0
        try:
            req=urllib.request.Request(url,headers={'Range':f'bytes={offset}-'} if offset else {})
            with urllib.request.urlopen(req,timeout=30) as response:
                append=offset and response.status==206
                if append and not response.headers.get('Content-Range','').startswith(f'bytes {offset}-'):raise ValueError('Rentang runtime tidak sesuai')
                with archive.open('ab' if append else 'wb') as output:
                    shutil.copyfileobj(response,output,length=262144)
            break
        except Exception:
            if attempt==2:raise
            print('Koneksi runtime terputus · mencoba melanjutkan',flush=True);time.sleep(attempt+1)
    with archive.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha512').digest() if hasattr(hashlib,'file_digest') else hashlib.sha512(stream.read()).digest()
    if digest!=expected:
        archive.unlink(missing_ok=True);raise ValueError('Checksum Deno tidak cocok; arsip ditolak')
    with tempfile.TemporaryDirectory(dir=root/'.runtime') as folder:
        new=Path(folder)/binary.name
        with tarfile.open(archive) as source:
            member=source.getmember(asset.get('member','package/deno'))
            if not member.isfile():raise ValueError('Binary runtime tidak valid')
            with source.extractfile(member) as stream,new.open('wb') as out:shutil.copyfileobj(stream,out)
        new.chmod(0o700)
        result=subprocess.run([str(new),'--version'],capture_output=True,text=True)
        if result.returncode or not result.stdout.startswith('deno '+version+' '):raise ValueError('Runtime Deno tidak dapat dijalankan')
        new.replace(binary)
    installed.write_text(json.dumps({'platform':platform_key(),'version':version}),encoding='utf-8')
    archive.unlink(missing_ok=True)

def setup(root,check=False,upgrade=False):
    if sysconfig.get_config_var('Py_GIL_DISABLED'):raise ValueError('Gunakan Python standar, bukan build free-threaded')
    if not (3,10)<=sys.version_info[:2]<=(3,14):raise ValueError('Gunakan Python standar 3.10–3.14 (3.13 direkomendasikan)')
    if not runtime_asset(json.loads((root/'runtime.json').read_text(encoding='utf-8'))):raise ValueError('OS/arsitektur belum didukung: '+platform_key())
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):raise ValueError('FFmpeg/FFprobe diperlukan. Pasang lewat apt (Ubuntu), Homebrew (macOS), atau winget Gyan.FFmpeg (Windows), lalu ulangi setup.')
    env=root/'.venv';py=python_path(root);marker=env/'.tool-identity.json';legacy=env/'.tool-location'
    identity=environment_identity(root)
    mismatch=False
    if env.exists():
        if marker.exists():mismatch=json.loads(marker.read_text(encoding='utf-8'))!=identity
        elif legacy.exists():mismatch=legacy.read_text(encoding='utf-8').strip()!=str(root) or not py.exists()
        else:mismatch=True
    if mismatch:
        if check:raise ValueError('Lingkungan Python perlu dibuat ulang setelah perubahan lokasi, OS, atau Python')
        old=root/'.state'/('venv-before-move-'+str(time.time_ns()));env.rename(old)
    if not py.exists():
        if check:raise ValueError('Belum ada lingkungan Python; jalankan setup.sh')
        print('Menyiapkan lingkungan Python lokal',flush=True);run_logged(root,[sys.executable,'-m','venv','--without-pip',str(env)])
    pip=subprocess.run([str(py),'-m','pip','--version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if pip.returncode:
        if check:raise ValueError('pip belum tersedia')
        wheel=root/'.bootstrap/pip-26.2.1-py3-none-any.whl'
        expected=(root/'.bootstrap/SHA256SUMS').read_text(encoding='utf-8').split()[0]
        if hashlib.sha256(wheel.read_bytes()).hexdigest()!=expected:raise ValueError('Checksum bootstrap pip tidak cocok')
        script='import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("pip",run_name="__main__")'
        run_logged(root,[str(py),'-c',script,str(wheel),'install','--no-index','--no-deps',str(wheel)])
    expected={}
    for line in (root/'requirements.lock').read_text(encoding='utf-8').splitlines():
        if '==' in line:
            name,version=line.split('==',1);expected[name]=version
    code='from importlib.metadata import version; import json,sys; expected=json.loads(sys.argv[1]); assert all(version(k)==v for k,v in expected.items())'
    ready=subprocess.run([str(py),'-c',code,json.dumps(expected)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if check and not ready:raise ValueError('Dependensi tidak sesuai rilis; jalankan setup.sh')
    if not check and (upgrade or not ready):
        print('Memasang dependensi rilis yang dipatok',flush=True)
        run_logged(root,[str(py),'-m','pip','install',*(['--upgrade'] if upgrade else []),'-r',str(root/'requirements.lock')])
    run_logged(root,[str(py),'-m','pip','check'])
    if check:
        binary=deno_path(root)
        if not binary.is_file():raise ValueError('Deno lokal belum tersedia; jalankan setup')
        stamp=root/'.runtime/installed.json'
        if not stamp.exists() or json.loads(stamp.read_text(encoding='utf-8')).get('platform')!=platform_key():raise ValueError('Runtime perlu diverifikasi untuk OS/arsitektur ini; jalankan setup')
        expected_runtime=json.loads((root/'runtime.json').read_text(encoding='utf-8'))['deno_version']
        result=subprocess.run([str(binary),'--version'],capture_output=True,text=True)
        if result.returncode or not result.stdout.startswith('deno '+expected_runtime+' '):raise ValueError('Deno tidak sesuai rilis')
    else:
        download_runtime(root);marker.write_text(json.dumps(identity,indent=2)+'\n',encoding='utf-8')
        legacy.write_text(str(root)+'\n',encoding='utf-8')
    for name in ('ffmpeg','ffprobe','deno'):
        binary=str(deno_path(root)) if name=='deno' else shutil.which(name)
        result=subprocess.run([binary,'--version' if name=='deno' else '-version'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
        if result.returncode:raise ValueError(name+' gagal dijalankan')
        print(name+' · '+result.stdout.splitlines()[0]+' · '+binary,flush=True)
    print(platform_key()+' · Python '+platform.python_version(),flush=True)
    print('Setup siap · FFmpeg, Deno, yt-dlp/EJS, dan menu tersedia',flush=True)

def program_files(source):
    names=[p.name for pattern in ('*.py','*.sh','*.cmd','*.md','*.svg') for p in source.glob(pattern)]
    if (source/'.gitignore').exists():names.append('.gitignore')
    names+=[n for n in ('youtube-downloader','requirements.txt','requirements.lock','runtime.json','release.json') if (source/n).is_file()]
    names+=[str(p.relative_to(source)) for p in (source/'.github').rglob('*') if p.is_file()]
    names+=[str(p.relative_to(source)) for p in (source/'tests').rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    names+=[str(p.relative_to(source)) for p in (source/'.bootstrap').glob('*') if p.is_file()]
    return sorted(set(names))

def supported_platforms(root):
    path=root/'release.json'
    return json.loads(path.read_text(encoding='utf-8'))['platforms'] if path.exists() else ['linux-x86_64','linux-aarch64']

def update(source,dest):
    if platform_key() not in supported_platforms(source):raise ValueError('Paket rilis tidak mendukung OS ini')
    if dest==source or not(dest/'app.py').exists():raise ValueError('Gunakan folder rilis baru terpisah dan tools tujuan yang sudah terpasang')
    names=program_files(source);backup=dest/'.state/program-backups'/str(time.time_ns());backup.mkdir(parents=True)
    existing=[];added=[]
    for name in names:
        if (dest/name).is_file():
            (backup/name).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest/name,backup/name);existing.append(name)
        else:added.append(name)
    (backup/'manifest.json').write_text(json.dumps({'files':existing,'added':added,'platforms':supported_platforms(dest)},indent=2)+'\n',encoding='utf-8')
    try:
        for name in names:
            target=dest/name;target.parent.mkdir(parents=True,exist_ok=True)
            tmp=target.with_name(target.name+'.new');shutil.copy2(source/name,tmp);tmp.replace(target)
        setup(dest)
        record={'backup':str(backup),'version':re.search(r"VERSION=['\"]([^'\"]+)",(source/'model.py').read_text(encoding='utf-8'))[1],'installed_at':time.time()}
        (dest/'.state/last-update.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    except BaseException:
        restore(dest,backup)
        raise
    print('Rilis terpasang. Cadangan: '+str(backup))

def restore(dest,backup):
    manifest=json.loads((backup/'manifest.json').read_text(encoding='utf-8'))
    if platform_key() not in manifest.get('platforms',['linux-x86_64','linux-aarch64']):raise ValueError('Cadangan program tidak mendukung OS ini; rollback ditolak tanpa mengubah program')
    for name in manifest['files']:
        target=dest/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(backup/name,target)
    for name in manifest['added']:(dest/name).unlink(missing_ok=True)
    setup(dest,upgrade=True)

def rollback(root):
    journal=root/'.state/last-update.json'
    record=json.loads(journal.read_text(encoding='utf-8'))
    backup=Path(record['backup']).resolve()
    if not backup.is_relative_to((root/'.state/program-backups').resolve()):raise ValueError('Lokasi cadangan tidak valid')
    if record.get('rolled_back_at'):raise ValueError('Cadangan ini sudah dipulihkan')
    restore(root,backup);record['rolled_back_at']=time.time();journal.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print('Program dan dependensi rilis dipulihkan. Media dan job dipertahankan.')

def migrate(source,dest):
    if dest==source or dest.is_relative_to(source) or (dest.exists() and any(dest.iterdir())):raise ValueError('Pilih folder kosong di luar sumber')
    shutil.copytree(source,dest,dirs_exist_ok=True,ignore=shutil.ignore_patterns('.venv','.runtime','__pycache__','run.lock','.git'))
    # Reuse an installed runtime; setup verifies its version/architecture before use.
    binary=deno_path(source)
    if binary.is_file() and (source/'.runtime/installed.json').is_file() and json.loads((source/'.runtime/installed.json').read_text(encoding='utf-8')).get('platform')==platform_key():
        target=deno_path(dest);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(binary,target);shutil.copy2(source/'.runtime/installed.json',dest/'.runtime/installed.json')
    journal=dest/'.state/last-update.json'
    if journal.exists():
        record=json.loads(journal.read_text(encoding='utf-8'));old=Path(record['backup'])
        if old.is_relative_to(source):record['backup']=str(dest/old.relative_to(source));journal.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    with guard(dest):setup(dest)
    print('Tools siap: '+str(dest));print('Media terpisah tetap di lokasi semula. Sumber tidak dihapus.')

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('action',choices=['setup','update','rollback','migrate','rebase-media']);p.add_argument('dest',nargs='?');p.add_argument('--check',action='store_true');p.add_argument('--upgrade',action='store_true');p.add_argument('--wait',action='store_true');p.add_argument('--from',dest='old_root');p.add_argument('--to',dest='new_root');p.add_argument('--dry-run',action='store_true');args=p.parse_args()
    try:
        if args.action in ('update','migrate') and not args.dest:raise ValueError('Folder tujuan diperlukan')
        target=Path(args.dest).expanduser().resolve() if args.dest else BASE
        guarded=target if args.action=='update' else BASE
        if args.wait:print('Menunggu kunci tools bebas',flush=True)
        with guard(guarded,args.wait):
            if args.action=='setup':setup(BASE,args.check,args.upgrade)
            elif args.action=='update':update(BASE,target)
            elif args.action=='migrate':migrate(BASE,target)
            elif args.action=='rebase-media':
                from media_migration import rebase_media
                if not args.old_root or not args.new_root:raise ValueError('Gunakan rebase-media --from OLD --to NEW [--dry-run]')
                rebase_media(BASE,args.old_root,args.new_root,args.dry_run)
            else:rollback(BASE)
        return 0
    except KeyboardInterrupt:print('Dihentikan',file=sys.stderr);return 130
    except Exception as exc:print('! GAGAL · '+str(exc),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
