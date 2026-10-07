"""Build the program-only release ZIP and its checksum on any supported OS."""
import argparse,hashlib,json,zipfile
from pathlib import Path
from maintenance import program_files

def build(source,dest):
    source=Path(source).resolve();dest=Path(dest).resolve();dest.mkdir(parents=True,exist_ok=True)
    version=json.loads((source/'release.json').read_text(encoding='utf-8'))['version']
    archive=dest/('youtube-downloader-'+version+'.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as out:
        for name in program_files(source):
            path=source/name;info=zipfile.ZipInfo('youtube-downloader/'+Path(name).as_posix(),(2026,1,1,0,0,0))
            mode=0o755 if path.name=='youtube-downloader' or path.suffix=='.sh' else 0o644
            info.create_system=3;info.external_attr=(0o100000|mode)<<16;info.compress_type=zipfile.ZIP_DEFLATED
            out.writestr(info,path.read_bytes())
    checksum=archive.with_suffix('.zip.sha256')
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n',encoding='utf-8')
    return archive,checksum

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('dest');args=p.parse_args()
    for path in build(Path(__file__).resolve().parent,args.dest):print(path)
