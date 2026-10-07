"""Explicit media-root rebasing with foreign path parsing and recovery backups."""
from contextlib import closing
import copy,json,os,shutil,sqlite3,time
from pathlib import Path,PurePosixPath,PureWindowsPath
from compat import native_output,platform_key


def source_path(value):
    win=PureWindowsPath(value)
    return win if win.drive else PurePosixPath(value)


def rebase_media(base,old_root,new_root,dry_run=False):
    base=Path(base);old=source_path(old_root);new=native_output(new_root)
    if not old.is_absolute():raise ValueError('--from harus berupa akar media absolut pada OS sumber')
    if not new.is_dir():raise ValueError('Salin media dahulu: folder --to belum tersedia')
    if str(old)==str(new):raise ValueError('Akar sumber dan tujuan sama')
    changed=0;destinations={};missing=[]
    def remap(value):
        nonlocal changed
        if isinstance(value,dict):return {k:remap(v) for k,v in value.items()}
        if isinstance(value,list):return [remap(v) for v in value]
        if isinstance(value,str):
            path=source_path(value)
            if type(path) is type(old) and path.is_absolute() and path.is_relative_to(old):
                relative=path.relative_to(old)
                if '..' in relative.parts:raise ValueError('Jalur media mengandung traversal ..')
                target=new.joinpath(*relative.parts).resolve()
                if not target.is_relative_to(new):raise ValueError('Jalur media keluar dari akar tujuan')
                key=str(target).casefold() if os.name=='nt' else str(target)
                previous=destinations.setdefault(key,str(path))
                if (previous.casefold() if isinstance(path,PureWindowsPath) else previous)!=(str(path).casefold() if isinstance(path,PureWindowsPath) else str(path)):raise ValueError('Benturan nama tujuan: '+str(target))
                changed+=1;return str(target)
        return value
    state=base/'.state';jobs=[]
    for path in sorted((state/'jobs').glob('*.json')):
        job=json.loads(path.read_text(encoding='utf-8'));updated=remap(job)
        if updated!=job:
            updated['browser']=None;updated['platform']=platform_key()
            jobs.append((path,updated))
    settings=base/'settings.json';config=None
    if settings.exists():
        original=json.loads(settings.read_text(encoding='utf-8'));config=remap(original)
        # Never carry a machine-specific Firefox profile through a cross-OS move.
        config['browser']=None
    database=state/'downloaded.sqlite3';rows=[];db=None
    try:
        if database.exists():
            db=sqlite3.connect(database)
            existing_rows=list(db.execute('SELECT rowid,variant,path,payload FROM results'))
            final_keys={}
            for rowid,variant,recorded,payload in existing_rows:
                original=json.loads(payload);result=remap(original)
                destination=(variant,str(result['path']).casefold() if os.name=='nt' else str(result['path']))
                if destination in final_keys and final_keys[destination]!=rowid:raise ValueError('Benturan arsip tujuan: '+result['path'])
                final_keys[destination]=rowid
                if original==result:continue
                for value in [result['path'],*[t['path'] for t in result.get('subtitles',[])]]:
                    if not Path(value).is_file():missing.append(value)
                rows.append((rowid,result))
        for before,after in sorted(destinations.items()):print('  '+after+' → '+before,flush=True)
        print(f'Rebase media · {old} → {new} · {changed} jalur · {len(jobs)} job · {len(rows)} hasil',flush=True)
        for value in sorted(set(missing)):print('! File tujuan belum ada: '+value,flush=True)
        if dry_run:return {'changed':changed,'jobs':len(jobs),'results':len(rows),'missing':sorted(set(missing))}
        if missing:raise ValueError('Media/SRT tujuan belum lengkap; tidak ada catatan yang diubah. Gunakan --dry-run.')
        if not changed:print('Tidak ada jalur yang perlu dipindahkan.',flush=True);return
        backup=state/'media-path-backups'/str(time.time_ns());backup.mkdir(parents=True)
        paths=[p for p,_ in jobs]+([settings] if settings.exists() else [])
        for path in paths:
            target=backup/path.relative_to(base);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        if db:
            with closing(sqlite3.connect(backup/'downloaded.sqlite3')) as saved:db.backup(saved)
        from store import write_json
        try:
            if db:db.execute('BEGIN IMMEDIATE')
            for rowid,result in rows:db.execute('UPDATE results SET path=?,payload=? WHERE rowid=?',(result['path'],json.dumps(result,ensure_ascii=False),rowid))
            for path,job in jobs:write_json(path,job)
            if config is not None:write_json(settings,config)
            if db:db.commit()
        except BaseException:
            if db:db.rollback()
            for path in paths:shutil.copy2(backup/path.relative_to(base),path)
            raise
        print('Jalur diperbarui. Cadangan: '+str(backup),flush=True)
    finally:
        if db:db.close()
