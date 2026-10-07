"""Explicit, limited online smoke test. Never run by unittest discovery."""
import json,sys,time
from pathlib import Path
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
from app import inspect_url
from model import pick,tracks,default_language,variant
from runner import run,WorkerError
from store import STATE,write_json,probe,lock

report=[];output=STATE/'smoke-media';started=time.time()
urls=[('single','https://www.youtube.com/watch?v=jNQXAC9IVRw',False),('short','https://www.youtube.com/shorts/18NGQq7p3LY',False),('playlist','https://www.youtube.com/playlist?list=PLt5yu3-wZAlSLRHmI1qNm0wjyVNWw1pCU',True)]
with lock():
    for kind,url,playlist in urls:
        row={'type':kind,'url':url};report.append(row)
        try:
            info=inspect_url(url,playlist=playlist)
            if playlist:
                row['entries']=len(info['entries']);row['title']=info['title']
                if not info['entries']:raise ValueError('Playlist kosong')
                info=inspect_url(info['entries'][0]['url'])
            row.update(title=info['title'],duration=info.get('duration'),formats=len(info['formats']))
            if (info.get('duration') or 999)>60:
                row['status']='metadata-only-duration-limit';print(kind,row['status'],flush=True);continue
            selected=tracks(info)
            selected=[next((t for t in selected if t['kind']=='manual'),selected[0])] if selected else []
            item={'id':info['id'],'title':info['title'],'url':info['webpage_url'],'choice':pick(info,quality='best'),'subtitles':selected}
            if kind=='single':
                translated=next((t for t in tracks(info) if t['kind']=='translated' and t['language']=='ja'),None)
                if translated:selected.append(translated)
            events=[];result=run({'action':'download','item':item,'output':str(output)},events.append)
            data=probe(result['path'],'video-audio')
            row.update(status='complete' if not result['missing'] else 'partial',quality=item['choice']['actual_quality'],dimensions=[item['choice']['width'],item['choice']['height']],result=result['path'],subtitle_count=len(result['subtitles']),missing_subtitles=len(result['missing']),stream_types=[s['codec_type'] for s in data['streams']],progress_events=sum(e['type']=='progress' for e in events))
            print(kind,row['status'],row['dimensions'],row['stream_types'],flush=True)
        except Exception as exc:
            row.update(status='failed',error=str(exc),category=getattr(exc,'category','system'));print(kind,'FAILED',row['category'],str(exc),flush=True)
            if row['category']=='authentication':break
        finally:write_json(STATE/'live-smoke.json',{'seconds':time.time()-started,'results':report})
print('Report:',STATE/'live-smoke.json',flush=True)
