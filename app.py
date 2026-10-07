"""Interactive YouTube downloader entry point."""
import argparse,os,sys,time,platform,subprocess,shutil
from compat import native_output,platform_key,deno_path,foreign_path
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from importlib.metadata import version
from model import VERSION,MODES,SelectionError,valid_url,qualities,languages,default_language,pick,tracks,container,variant,basename,item_indices,has_video,has_audio,resolution
from store import BASE,STATE,lock,settings,new_job,load_job,save_job,Archive
from runner import run,WorkerError
from ui import UI,Session,Cancelled,BLUE,RED,size,duration

def inspect_url(url,browser=None,playlist=False):return run({'action':'inspect','url':url,'browser':browser,'playlist':playlist})

def format_label(f):
    quality=str(resolution(f))+'p · '+str(f.get('width'))+'×'+str(f.get('height')) if has_video(f) else 'Audio'
    return f"{f['format_id']} · {quality} · {f.get('fps') or '—'} fps · {f.get('dynamic_range') or 'SDR'} · {f.get('vcodec') or '—'} / {f.get('acodec') or '—'} · {f.get('language') or 'und'} · {f.get('abr') or f.get('tbr') or '—'} kbps"

def wizard(ui,url,browser=None,output=None):
    url=valid_url(url or ui.text('Tempel link YouTube:'))
    qs=parse_qs(urlparse(url).query)
    is_playlist=('list' in qs or urlparse(url).path=='/playlist')
    if is_playlist and ('v' in qs or urlparse(url).hostname=='youtu.be'):
        is_playlist=ui.choose('Link berisi video dan playlist. Ambil yang mana?',[('Video ini saja',False),('Playlist',True)])
    with ui.activity('Mengambil informasi YouTube'):initial=inspect_url(url,browser,is_playlist)
    if initial.get('playlist'):
        entries=initial['entries']
        if not entries:raise ValueError('Playlist kosong atau tidak dapat diakses')
        ui.say(f"{initial['title']} · {len(entries)} item",BLUE)
        selection=ui.choose('Pilih item playlist',[('Semua item','all'),('Pilih dengan checkbox','checkbox'),('Nomor/rentang','range')])
        if selection=='all':selected=entries
        elif selection=='checkbox':selected=ui.checkboxes('Space pilih · daftar bisa digulir',[(f"{i+1}. {e['title']}",e) for i,e in enumerate(entries)])
        else:
            while True:
                try:selected=[entries[i] for i in item_indices(ui.text('Nomor/rentang, misalnya 1,3,5-10:'),len(entries))];break
                except SelectionError as exc:ui.say(exc,RED)
        if not selected:raise Cancelled()
        metadata=[];unavailable=[];errors=0
        with ui.activity(f'Memeriksa kualitas dan subtitle {len(selected)} item'):
            for entry in selected:
                try:
                    info=inspect_url(entry['url'],browser)
                    metadata.append(info);errors=0
                except WorkerError as exc:
                    unavailable.append({'title':entry['title'],'reason':str(exc)})
                    if exc.category=='authentication':raise
                    if exc.category not in ('unavailable',):errors+=1
                    if errors>=3:raise WorkerError('Tiga kegagalan metadata berurutan; batch dihentikan. '+str(exc),exc.category)
        if unavailable:ui.table(('Item tidak tersedia','Alasan'),[(x['title'],x['reason']) for x in unavailable])
    else:metadata=[initial]
    if not metadata:raise ValueError('Tidak ada item yang dapat diunduh')
    if len(metadata)==1:
        info=metadata[0];qsizes=qualities(info)
        ui.say(f"{info['title']} · {duration(info.get('duration') or 0)} · {info.get('channel') or info.get('uploader') or 'YouTube'}",BLUE)
        largest=next((f for f in reversed(info['formats']) if has_video(f)),None)
        if largest:ui.say(('Portrait' if largest['height']>largest['width'] else 'Landscape')+f" · {largest['width']} × {largest['height']}")
    mode=ui.choose('Apa yang ingin diunduh?',[('Video + audio','video-audio'),('Audio saja','audio'),('Video saja — tanpa suara','video')])
    quality='best';manual=False;audio_format='original'
    if mode!='audio':
        tiers=sorted(set(q for m in metadata for q in qualities(m)),reverse=True)
        if not tiers:raise ValueError('Tidak ada format video yang tersedia')
        recommended=1080 if 1080 in tiers else max((q for q in tiers if q<=1080),default=min(tiers))
        options=[('Kualitas maksimal'+(' + audio terbaik' if mode=='video-audio' else '')+' · Direkomendasikan','best')]
        options+=[(f'{q}p'+(' · pilihan harian' if q==recommended else '')+(' + audio terbaik' if mode=='video-audio' else ''),q) for q in tiers]
        options+=[('Pilih format secara detail per item','manual')]
        quality=ui.choose('Pilih kualitas (playlist boleh turun kualitas, tidak naik otomatis)',options,default='best')
        manual=quality=='manual'
        if manual:quality='best'
    lang='original'
    if mode!='video':
        available=sorted(set(lang for m in metadata for lang in languages(m)))
        lang=ui.choose('Bahasa audio',[('Bahasa asli per video','original')]+[(x,x) for x in available])
        if mode=='audio':
            audio_format=ui.choose('Format audio',[('Audio asli terbaik · tanpa konversi','original'),('MP3 · konversi lossy','mp3')])
            manual=ui.choose('Pemilihan kualitas audio',[('Audio terbaik otomatis',False),('Pilih format/bitrate per item',True)])
    all_tracks={}
    for m in metadata:
        for t in tracks(m):
            identity=(t['language'],t['kind']) if len(metadata)>1 else (t['language'],t['kind'],t['key'],t['origin'])
            all_tracks.setdefault(identity,t)
    chosen=[]
    if all_tracks:
        addon=ui.choose('Tambahan subtitle',[('Tanpa subtitle',False),('Pilih bahasa/track subtitle',True)])
        if addon:
            names={'manual':'CC asli','automatic':'Caption otomatis','translated':'Terjemahan otomatis'}
            chosen=ui.checkboxes('Subtitle · file SRT + track; audio saja menyimpan file SRT',[(f"{t['name']} · {names[t['kind']]}"+(' · sumber dipilih per video' if len(metadata)>1 and t['kind']=='translated' else ''),identity) for identity,t in all_tracks.items()])
    else:ui.say('Tidak ada subtitle yang dikembalikan YouTube; tambahan subtitle tidak tersedia.')
    items=[];rows=[]
    for m in metadata:
        language=default_language(m) if lang=='original' else lang
        video_id=None;audio_id=None
        if manual:
            if mode!='audio':video_id=ui.choose('Format video · '+m['title'],[(format_label(f),f['format_id']) for f in reversed(m['formats']) if has_video(f) and not f.get('has_drm')])
            if mode!='video':
                af=[f for f in reversed(m['formats']) if has_audio(f) and (f.get('language') or 'und')==language]
                if af:audio_id=ui.choose('Format audio · '+m['title'],[(format_label(f),f['format_id']) for f in af])
        try:choice=pick(m,mode,quality,language,audio_format,video_id,audio_id)
        except SelectionError as exc:rows.append((m['title'],'DILEWATI',str(exc)));continue
        selected_tracks=[]
        current_tracks=tracks(m)
        for identity in chosen:
            candidates=[t for t in current_tracks if (t['language'],t['kind'])==identity[:2]]
            if len(identity)>2:candidates=[t for t in candidates if (t['key'],t['origin'])==identity[2:]]
            if candidates:
                selected_tracks.append(next((t for t in candidates if t['source']==default_language(m)),candidates[0]))
            else:
                expected=all_tracks[identity]
                # Keep requested tracks so absence is recorded instead of silently disappearing.
                selected_tracks.append(expected)
        item={'id':m['id'],'title':m['title'],'url':m.get('webpage_url') or 'https://www.youtube.com/watch?v='+m['id'],'choice':choice,'subtitles':selected_tracks,'status':'pending'}
        items.append(item)
        actual=f"{choice['actual_quality']}p · {choice['width']}×{choice['height']} · {choice['fps'] or '—'}fps · {choice.get('dynamic_range') or 'SDR'}" if mode!='audio' else choice['audio_format']
        if quality!='best' and choice['actual_quality'] and choice['actual_quality']!=quality:actual+=f' · turun dari {quality}p'
        details=f"{container(choice,selected_tracks).upper()} · audio {language if mode!='video' else 'tanpa suara'} · "+('≈ '+size(choice['estimated_size']) if choice['estimated_size'] else 'ukuran belum diketahui')
        if selected_tracks:details+=' · subtitle '+', '.join(t['name'] for t in selected_tracks)
        rows.append((m['title'],actual,details))
    if not items:raise ValueError('Tidak ada item yang memenuhi pilihan')
    ui.table(('Video','Kualitas','Hasil'),rows)
    root=str(native_output(output or settings()['output']));ui.say('Hasil: '+root,BLUE)
    if ui.choose('Mulai download?',[('Mulai',True),('Batal',False)]) is False:raise Cancelled()
    job=new_job(items,root,browser);ui.say('Job tersimpan: '+job['id'],BLUE)
    return job

def execute(ui,job):
    native_output(job['output'])
    if job.get('browser') and ((job.get('platform') and job['platform'].split('-')[0]!=platform_key().split('-')[0]) or (':' in job['browser'] and foreign_path(job['browser'].split(':',1)[1]))):
        raise ValueError('Sesi Firefox berasal dari OS lain. Jalankan resume dengan --browser firefox pada perangkat ini.')
    archive=Archive();consecutive=0;job['status']='running';save_job(job)
    try:
        with Session(ui,len(job['items'])) as session:
            try:
                for item in job['items']:
                    key=variant(item);previous=archive.get(key,item['choice']['mode'],job['output'])
                    if previous and not previous.get('missing'):
                        item['status']='complete';item['result']=previous;session.item_end('skipped');save_job(job);consecutive=0;continue
                    request={'action':'download','item':item,'output':job['output'],'browser':job.get('browser')}
                    if previous:request['existing_file']=previous['path']
                    item['status']='running';save_job(job)
                    try:
                        result=run(request,session.event);archive.put(key,result)
                        item['result']=result;item['status']='partial' if result.get('missing') else 'complete'
                        item.pop('error',None);session.item_end(item['status']);consecutive=0
                    except WorkerError as exc:
                        item['status']='failed';item['error']={'message':str(exc),'category':exc.category}
                        session.event({'type':'failure','message':'! GAGAL · '+str(exc)});session.item_end('failed')
                        if exc.category!='unavailable':consecutive+=1
                        else:consecutive=0
                        save_job(job)
                        if exc.category=='authentication' or consecutive>=3:
                            job['status']='blocked';raise ValueError('Batch dihentikan. '+('Login di Firefox lalu ulangi dengan pilihan sesi yang benar.' if exc.category=='authentication' else 'Tiga kegagalan sistem berurutan; periksa log/dependensi sebelum resume.'))
                    save_job(job)
            except (KeyboardInterrupt,Cancelled):job['status']='interrupted';raise
            except Exception:
                if job['status']=='running':job['status']='blocked'
                raise
        job['status']='complete' if all(x['status']=='complete' for x in job['items']) else 'partial'
    finally:
        save_job(job);archive.close()
        ui.say('Job: '+job['id']+' · '+job['status']);ui.say('Hasil: '+job['output']);ui.say('Log: '+str(STATE/'download.log'))
    if job['status']!='complete':raise ValueError('Ada item/subtitle belum selesai; resume '+job['id']+' untuk mencoba lagi.')

def status(ui):
    rows=[('Versi',VERSION),('Platform',platform_key()),('Python',platform.python_version()+' · '+sys.executable),('Tools',str(BASE)),('Media',str(Path(settings()['output']).expanduser())),('Catatan',str(STATE))]
    from pip._vendor.packaging.requirements import Requirement
    for line in (BASE/'requirements.lock').read_text(encoding='utf-8').splitlines():
        if '==' not in line:continue
        dependency=Requirement(line)
        if dependency.marker and not dependency.marker.evaluate():continue
        try:
            actual=version(dependency.name);rows.append((dependency.name,actual+(' · TIDAK SESUAI '+str(dependency.specifier) if actual not in dependency.specifier else '')))
        except Exception:rows.append((dependency.name,'belum terpasang'))
    for name in ('ffmpeg','ffprobe','deno'):
        binary=str(deno_path(BASE)) if name=='deno' else shutil.which(name)
        try:
            r=subprocess.run([binary,'--version' if name=='deno' else '-version'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
            rows.append((name,(r.stdout.splitlines() or ['tidak tersedia'])[0]+' · '+str(binary)))
        except (OSError,TypeError,subprocess.TimeoutExpired):rows.append((name,'tidak tersedia · jalankan setup --check'))
    ui.table(('Informasi','Nilai'),rows)
    jobs=sorted((STATE/'jobs').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10] if (STATE/'jobs').exists() else []
    if jobs:ui.table(('Job','Status','Item'),[(j['id'],j['status'],len(j['items'])) for j in (load_job(p.stem) for p in jobs)])

def preview(ui):
    ui.say('YouTube Downloader · preview simulasi',BLUE)
    with Session(ui,2) as session:
        for index,stream in enumerate(('video','audio')):
            session.event({'type':'start','name':'Contoh '+stream,'stream':stream})
            for i in range(0,101,5):
                session.event({'type':'progress','downloaded':i*1048576,'total':104857600,'speed':10*1048576,'eta':(100-i)/10});time.sleep(.035)
            session.event({'type':'file_done'})
            if index==0:
                session.event({'type':'start','name':'Ukuran belum diketahui','stream':'media'})
                time.sleep(.25);session.event({'type':'wait','message':'Menunggu · simulasi','until':time.time()+.3});time.sleep(.3)
            session.event({'type':'stage','message':'Menggabungkan · simulasi'});time.sleep(.2);session.item_end('complete')
    ui.say('Preview selesai; tidak ada jaringan, file, atau download yang dibuat.')

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description='Download YouTube: pilih video/audio, kualitas, dan subtitle.')
    parser.add_argument('action',nargs='?',help='Link YouTube, status, preview, atau resume')
    parser.add_argument('job',nargs='?',help='ID job untuk resume')
    parser.add_argument('--plain',action='store_true');parser.add_argument('--browser',help='Opsional: firefox atau firefox:/jalur/profil')
    parser.add_argument('--output',help='Folder hasil alternatif')
    args=parser.parse_args();ui=UI(args.plain)
    try:
        if args.action=='preview':preview(ui);return 0
        if args.action=='status':status(ui);return 0
        ui.say('YouTube Downloader · '+VERSION,BLUE)
        if args.browser and not(args.browser=='firefox' or args.browser.startswith('firefox:')):raise ValueError('Pilihan browser: firefox atau firefox:/jalur/profil')
        with lock():
            if args.action=='resume':
                if not args.job:raise ValueError('Gunakan resume ID_JOB')
                job=load_job(args.job)
                if args.browser:job['browser']=args.browser;job['platform']=platform_key()
                if args.output and str(Path(args.output).expanduser().resolve())!=job['output']:raise ValueError('Output job tidak dapat diubah saat resume')
            else:
                if args.job:raise ValueError('Argumen tambahan tidak valid')
                if not sys.stdin.isatty():raise ValueError('Wizard membutuhkan terminal. Gunakan resume ID_JOB untuk job yang sudah tersimpan.')
                job=wizard(ui,args.action,args.browser or settings().get('browser'),args.output)
            execute(ui,job)
        return 0
    except (KeyboardInterrupt,Cancelled):ui.say('Dihentikan; job yang sudah tersimpan dapat dilanjutkan dengan resume ID_JOB.',BLUE);return 130
    except Exception as exc:ui.say('! GAGAL · '+str(exc),RED);ui.say('Log: '+str(STATE/'download.log'));return 1
if __name__=='__main__':sys.exit(main())
