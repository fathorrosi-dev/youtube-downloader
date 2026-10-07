"""Selection policy; no network, shell or filesystem mutations."""
import hashlib
import json
import re
from urllib.parse import urlparse,parse_qs

VERSION='1.1.0'
MODES=('video-audio','audio','video')

class SelectionError(ValueError): pass

def resolution(f):
    values=[int(f[k]) for k in ('width','height') if f.get(k)]
    return min(values) if len(values)==2 else int(f.get('height') or 0)

def has_video(f):return f.get('vcodec','none') not in ('none',None) and resolution(f)>0

def has_audio(f):return f.get('acodec','none') not in ('none',None)

def languages(info):
    return sorted({f.get('language') or 'und' for f in info.get('formats',[]) if has_audio(f)})

def default_language(info):
    fs=[f for f in info.get('formats',[]) if has_audio(f)]
    original=info.get('language')
    if original and original in languages(info):return original
    if not fs:return 'und'
    preferred=max(fs,key=lambda f:(f.get('language_preference') or 0))
    return preferred.get('language') or 'und'

def qualities(info):return sorted({resolution(f) for f in info.get('formats',[]) if has_video(f)},reverse=True)

def pick(info,mode='video-audio',quality=1080,language=None,audio_format='original',video_id=None,audio_id=None):
    fs=[f for f in info.get('formats',[]) if f.get('format_id') and not f.get('has_drm')]
    language=language or default_language(info)
    v=None;a=None
    if mode not in MODES:raise SelectionError('Mode tidak valid')
    if mode!='audio':
        vs=[f for f in fs if has_video(f)]
        if video_id:vs=[f for f in vs if f['format_id']==video_id]
        elif quality!='best':
            # An exact tier wins; playlist fallback uses only lower tiers.
            exact=[f for f in vs if resolution(f)==int(quality)]
            vs=exact or [f for f in vs if resolution(f)<int(quality)]
        if not vs:raise SelectionError('Tidak ada video pada atau di bawah target kualitas')
        tier=max(resolution(f) for f in vs)
        # Metadata formats retain yt-dlp's native worst-to-best ordering.
        v=[f for f in vs if resolution(f)==tier][-1]
    if mode!='video':
        af=[f for f in fs if has_audio(f) and (f.get('language') or 'und')==language]
        if audio_id:af=[f for f in af if f['format_id']==audio_id]
        if not af:raise SelectionError('Track audio yang dipilih tidak tersedia')
        only=[f for f in af if not has_video(f)]
        a=(only or af)[-1]
    streams={f['format_id']:f for f in (v,a) if f}
    sizes=[f.get('filesize') or f.get('filesize_approx') for f in streams.values()]
    estimate=sum(sizes) if sizes and all(sizes) else 0
    return {'mode':mode,'quality':quality,'actual_quality':resolution(v) if v else None,
            'language':language,'audio_format':audio_format,'video_format':v['format_id'] if v else None,
            'audio_format_id':a['format_id'] if a else None,
            'width':v.get('width') if v else None,'height':v.get('height') if v else None,
            'fps':v.get('fps') if v else None,'vcodec':v.get('vcodec') if v else None,
            'dynamic_range':v.get('dynamic_range') if v else None,
            'acodec':a.get('acodec') if a else None,
            'estimated_size':estimate}

def tracks(info):
    rows=[];seen=set()
    for origin,kind in (('subtitles','manual'),('automatic_captions','automatic')):
        for key,formats in info.get(origin,{}).items():
            if key=='live_chat' or not formats:continue
            qs=parse_qs(urlparse(formats[0].get('url','')).query)
            translated=bool(qs.get('tlang') and qs.get('tlang')!=qs.get('lang'))
            source=qs.get('lang',[''])[0]
            target=qs.get('tlang',[re.sub(r'-orig$','',key)])[0]
            # Suppress duplicate aliases of the same automatic original track.
            identity=(kind,target,source,translated)
            if identity in seen:continue
            seen.add(identity)
            rows.append({'key':key,'origin':origin,'kind':'translated' if translated else kind,
                         'language':target,'source':source,'name':formats[0].get('name') or key})
    return rows

def container(choice,subtitles):
    if choice['mode']=='audio':
        if choice['audio_format']=='mp3':return 'mp3'
        codec=choice.get('acodec') or ''
        return 'm4a' if codec.startswith(('mp4a','aac','alac')) else 'opus' if codec.startswith('opus') else 'ogg' if codec.startswith('vorbis') else 'mp3' if codec.startswith('mp3') else 'mka'
    if subtitles:return 'mkv'
    compatible_video=(choice.get('vcodec') or '').startswith(('avc','h264','hevc','h265','hev','hvc','av01','av1','mpeg4'))
    compatible_audio=choice['mode']=='video' or (choice.get('acodec') or '').startswith(('mp4a','aac','alac'))
    return 'mp4' if compatible_video and compatible_audio else 'mkv'

def variant(item):
    identity={'id':item['id'],'choice':item['choice'],'subtitles':item.get('subtitles',[])}
    # Estimated sizes do not identify a media variant.
    identity['choice']={k:v for k,v in identity['choice'].items() if k not in ('estimated_size','quality')}
    return hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=True).encode()).hexdigest()

def basename(item):
    title=re.sub(r'[\x00-\x1f/\\:*?"<>|]','_',item.get('title') or item['id']).strip(' .')[:100] or 'video'
    title=title.encode('utf-8')[:140].decode('utf-8',errors='ignore')
    choice=item['choice'];quality=str(choice['actual_quality'])+'p' if choice['actual_quality'] else choice['audio_format']
    return f"{title} [{item['id']}] - {quality} - {choice['mode']} - {variant(item)[:10]}"

def item_indices(text,total):
    if text in ('all','semua',''):return list(range(total))
    result=set()
    for part in text.split(','):
        m=re.fullmatch(r'\s*(\d+)(?:\s*-\s*(\d+))?\s*',part)
        if not m:raise SelectionError('Gunakan nomor/rentang, misalnya 1,3,5-10')
        first=int(m[1]);last=int(m[2] or m[1])
        if first<1 or last<first or last>total:raise SelectionError('Nomor item di luar daftar')
        result.update(range(first-1,last))
    return sorted(result)

def valid_url(url):
    parsed=urlparse(url)
    host=(parsed.hostname or '').lower()
    if parsed.scheme not in ('http','https') or host not in ('youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be'):
        raise SelectionError('Masukkan link YouTube yang valid')
    if host!='youtu.be' and parsed.path not in ('/watch','/playlist') and not parsed.path.startswith(('/shorts/','/embed/','/live/')):
        raise SelectionError('Gunakan link video, Shorts, atau playlist; seluruh kanal belum didukung')
    return url

def classify(message):
    low=str(message).lower()
    if any(x in low for x in ('sign in','log in','authentication','confirm your age','not a bot','login required')):return 'authentication'
    if any(x in low for x in ('private video','video is private','video unavailable','video is unavailable','has been removed','not available','deleted video','copyright','members-only')):return 'unavailable'
    if '429' in low or 'too many requests' in low:return 'rate_limit'
    if any(x in low for x in ('timed out','timeout','connection','network','temporary failure','503','502')):return 'network'
    return 'system'
