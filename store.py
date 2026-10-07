"""Private job checkpoints and profile-aware archive."""
from contextlib import contextmanager,closing
from pathlib import Path
import json,sqlite3,uuid,subprocess,os
from datetime import datetime,timezone
from compat import file_lock,default_media,platform_key

BASE=Path(__file__).resolve().parent
STATE=BASE/'.state'
DEFAULT={'output':default_media(),'browser':None}

def now():return datetime.now(timezone.utc).isoformat()

def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    temp.replace(path)

def lock(wait=False):return file_lock(STATE/'run.lock',wait)


def settings():
    path=BASE/'settings.json'
    return {**DEFAULT,**(json.loads(path.read_text(encoding='utf-8')) if path.exists() else {})}

def save_job(job):
    job['updated_at']=now();write_json(STATE/'jobs'/f"{job['id']}.json",job)

def new_job(items,output,browser=None):
    job={'id':uuid.uuid4().hex[:12],'created_at':now(),'status':'prepared','platform':platform_key(),'output':str(Path(output).expanduser().resolve()),'browser':browser,'items':items}
    save_job(job);return job

def load_job(identifier):
    if not identifier.isalnum():raise ValueError('ID job tidak valid')
    path=STATE/'jobs'/f'{identifier}.json'
    if not path.exists():raise ValueError('Job tidak ditemukan')
    return json.loads(path.read_text(encoding='utf-8'))

def probe(path,mode):
    result=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,encoding="utf-8",timeout=30)
    if result.returncode:raise ValueError('FFprobe menolak hasil media')
    data=json.loads(result.stdout);streams=data.get('streams',[])
    video=any(s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic') for s in streams)
    audio=any(s.get('codec_type')=='audio' for s in streams)
    if mode=='video-audio' and not(video and audio):raise ValueError('Hasil harus memiliki video dan audio')
    if mode=='video' and (not video or audio):raise ValueError('Hasil video saja harus tanpa audio')
    if mode=='audio' and (not audio or video):raise ValueError('Hasil audio saja tidak valid')
    if not Path(path).stat().st_size:raise ValueError('Hasil media kosong')
    return data

class Archive:
    def __init__(self):
        STATE.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(STATE/'downloaded.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS results (variant TEXT NOT NULL,path TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(variant,path))');self.db.commit()
    def close(self):self.db.close()
    def get(self,key,mode,output=None):
        for row in self.db.execute('SELECT payload FROM results WHERE variant=? ORDER BY rowid DESC',(key,)):
            result=json.loads(row[0]);path=Path(result['path'])
            if output and not path.resolve().is_relative_to(Path(output).resolve()):continue
            if not path.is_file():continue
            try:probe(path,mode)
            except (ValueError,subprocess.SubprocessError):continue
            missing=[track for track in result.get('subtitles',[]) if not Path(track['path']).is_file()]
            if missing:result['missing']=list({x['key']:x for x in result.get('missing',[])+missing}.values())
            return result
        return None
    def put(self,key,result):
        probe(result['path'],result['mode'])
        with self.db:self.db.execute('INSERT OR REPLACE INTO results VALUES (?,?,?)',(key,str(Path(result['path']).resolve()),json.dumps(result,ensure_ascii=False)))
