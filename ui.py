"""Menus and bounded terminal display; no downloader output parsing."""
from contextlib import contextmanager
import os,sys,time
from rich.console import Console,Group
from rich.live import Live
from rich.measure import Measurement
from rich.progress import Progress,ProgressColumn,TextColumn
from rich.table import Table,Column
from rich.text import Text
from rich.spinner import Spinner
import questionary

PINK = '#FFC2D4'
RED = '#E8384F'
BLUE = '#A8D8F0'
DIM = '#94A3B8'
EMPTY = '#334155'


class AnimationClock:
    """Animation time advances only while a file is actively downloading."""
    def __init__(self):
        self.elapsed = 0.0
        self.resumed_at = None

    def __call__(self):
        return self.elapsed + (time.monotonic() - self.resumed_at
                               if self.resumed_at is not None else 0)

    def active(self, value, reset=False):
        self.elapsed = 0.0 if reset else self()
        self.resumed_at = time.monotonic() if value else None


class SolidBar:
    """A one-cell-high bar measured against the space Rich allocates to it."""
    def __init__(self, completed, total, color, animation_time=0, indicator=False):
        self.completed, self.total, self.color = completed, total, color
        self.animation_time, self.indicator = animation_time, indicator

    def text(self, width):
        width = max(0, width)
        bar = Text(no_wrap=True, overflow='crop')
        if self.total is None:
            count = min(3, width) if self.indicator else 0
            travel = width - count
            step = int(self.animation_time * 8) % (2 * travel) if travel else 0
            start = min(step, 2 * travel - step) if travel else 0
            bar.append('░' * start, EMPTY)
            bar.append('█' * count, self.color)
            bar.append('░' * (width - start - count), EMPTY)
            return bar
        ratio = min(1.0, max(0.0, self.completed / self.total)) if self.total > 0 else 0
        eighths = int(ratio * width * 8)
        full, fraction = divmod(eighths, 8)
        bar.append('█' * full, self.color)
        if fraction:
            # A dark background keeps the fractional cell aligned with the track.
            bar.append('▏▎▍▌▋▊▉'[fraction - 1], f'{self.color} on {EMPTY}')
        bar.append('░' * (width - full - bool(fraction)), EMPTY)
        return bar

    def __rich_measure__(self, console, options):
        return Measurement(1, max(1, options.max_width))

    def __rich_console__(self, console, options):
        yield self.text(options.max_width)


class SolidBarColumn(ProgressColumn):
    def __init__(self, color, clock=None):
        super().__init__(table_column=Column(ratio=1, no_wrap=True))
        self.color, self.clock = color, clock

    def render(self, task):
        return SolidBar(task.completed, task.total, self.color,
                        self.clock() if self.clock else 0,
                        task.fields.get('indicator', False))


def duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}j {minutes}m' if hours else f'{minutes}m {seconds}d' if minutes else f'{seconds}d'


def size(number):
    number = max(0, number or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if number < 1024 or unit == 'TB':
            return f'{number:.1f} {unit}' if unit != 'B' else f'{number:.0f} B'
        number /= 1024


class Cancelled(Exception):pass

class UI:
    def __init__(self,plain=False):
        self.plain=plain or not(sys.stdout.isatty() and sys.stderr.isatty()) or os.environ.get('TERM')=='dumb'
        self.console=Console(stderr=True,force_terminal=not self.plain,color_system='truecolor' if not self.plain else None,no_color='NO_COLOR' in os.environ,highlight=False)
        self.style=questionary.Style([('qmark',PINK),('question',BLUE),('answer',PINK),('pointer',BLUE),('highlighted',BLUE),('selected',PINK)]) if 'NO_COLOR' not in os.environ else questionary.Style([])

    def say(self,message,color=None):
        if self.plain:
            sys.stderr.write(str(message)+'\n');sys.stderr.flush()
        else:self.console.print(Text(str(message),style=color,overflow='fold'))

    def choose(self,title,options,default=None):
        if not sys.stdin.isatty():raise ValueError('Menu membutuhkan input terminal. Gunakan terminal interaktif atau resume ID_JOB.')
        if self.plain:
            self.say(title,BLUE)
            for i,(label,_) in enumerate(options,1):self.say(f'{i}. {label}')
            while True:
                self.say('Nomor pilihan (q untuk batal):')
                answer=input().strip()
                if answer.lower()=='q':raise Cancelled()
                if answer.isdigit() and 1<=int(answer)<=len(options):return options[int(answer)-1][1]
                self.say('Pilihan tidak valid',RED)
        values=[questionary.Choice(label,value=value) for label,value in options]
        result=questionary.select(title,choices=values,default=default,style=self.style).unsafe_ask()
        if result is None:raise Cancelled()
        return result

    def text(self,title,default=''):
        if not sys.stdin.isatty():raise ValueError('Masukkan link melalui terminal interaktif.')
        if self.plain:self.say(title,BLUE);return input().strip() or default
        result=questionary.text(title,default=default,style=self.style).unsafe_ask()
        if result is None:raise Cancelled()
        return result.strip()

    def checkboxes(self,title,options):
        if self.plain:
            self.say(title,BLUE)
            for i,(label,_) in enumerate(options,1):self.say(f'{i}. {label}')
            from model import item_indices
            while True:
                answer=self.text('Nomor/rentang, all, atau none untuk tidak memilih:')
                if answer=='none':return []
                try:return [options[i][1] for i in item_indices(answer,len(options))]
                except ValueError as exc:self.say(exc,RED)
        result=questionary.checkbox(title,choices=[questionary.Choice(label,value=value) for label,value in options],style=self.style,instruction='Space pilih · Enter lanjut · Ctrl+C batal').unsafe_ask()
        if result is None:raise Cancelled()
        return result

    def table(self,headers,rows):
        if self.plain:
            self.say(' | '.join(headers),BLUE)
            for row in rows:self.say(' | '.join(map(str,row)))
            return
        table=Table(box=None,show_edge=False,padding=(0,1))
        for header in headers:table.add_column(header,header_style=BLUE,overflow='fold')
        for row in rows:table.add_row(*(Text(str(v)) for v in row))
        self.console.print(table)

    @contextmanager
    def activity(self,message):
        if self.plain:self.say(message);yield None;return
        with Live(Spinner('dots',text=Text(message,style=BLUE),style=PINK),console=self.console,transient=True,refresh_per_second=4,redirect_stdout=False,redirect_stderr=False):yield None

class Session:
    def __init__(self,ui,total):
        self.ui=ui;self.total=total;self.done=0;self.saved=0;self.skipped=0;self.failed=0
        self.started=time.monotonic();self.clock=AnimationClock();self.message='Menyiapkan'
        self.current='';self.downloaded=0;self.byte_total=None;self.speed=0;self.eta=None;self.until=None
        self.overall=Progress(TextColumn('Item'),SolidBarColumn(PINK),TextColumn('{task.percentage:.0f}% · {task.completed:.0f}/{task.total:.0f}'),expand=True)
        self.oid=self.overall.add_task('',total=total)
        self.file=Progress(TextColumn('File'),SolidBarColumn(RED,self.clock),TextColumn('{task.fields[label]}'),expand=True)
        self.fid=self.file.add_task('',total=None,label='Menyiapkan',indicator=False)
        self.live=None;self.last_plain=None

    def __enter__(self):
        if not self.ui.plain:
            self.live=Live(self,console=self.ui.console,transient=True,refresh_per_second=4,redirect_stdout=False,redirect_stderr=False);self.live.start()
        else:self.ui.say(f'Download · {self.total} item')
        return self

    def event(self,event):
        kind=event.get('type')
        if kind=='start':
            self.current=event.get('name','');self.downloaded=0;self.byte_total=None;self.speed=0;self.eta=None;self.until=None
            self.clock.active(True,reset=True);self.file.reset(self.fid,total=None,indicator=True);self.message='Mengunduh '+event.get('stream','media')
        elif kind=='progress':
            self.clock.active(True);self.until=None;self.message='Mengunduh media';self.downloaded=event.get('downloaded') or 0;self.byte_total=event.get('total') or None
            self.speed=event.get('speed') or 0;self.eta=event.get('eta')
            self.file.update(self.fid,completed=self.downloaded,total=self.byte_total,indicator=True)
            self.file.tasks[0].total=self.byte_total
        elif kind=='file_done':
            self.downloaded=event.get('downloaded') or event.get('total') or self.byte_total or self.downloaded
            self.byte_total=event.get('total') or self.downloaded or None;self.eta=None;self.until=None
            self.clock.active(False);self.file.update(self.fid,total=max(1,self.downloaded),completed=max(1,self.downloaded));self.message='File sementara selesai'
        elif kind in ('stage','retry','wait','failure','notice'):
            self.clock.active(False);self.until=event.get('until') if kind=='wait' else None
            self.message=event.get('message') or ('Mencoba kembali' if kind=='retry' else '! GAGAL' if kind=='failure' else 'Menunggu')
        if self.ui.plain and kind in ('start','stage','retry','wait','failure','notice') and self.message!=self.last_plain:
            self.ui.say(self.message,RED if kind=='failure' else None);self.last_plain=self.message

    def item_end(self,status):
        self.clock.active(False);self.done+=1
        if status in ('complete','partial'):self.saved+=1
        elif status=='skipped':self.skipped+=1
        else:self.failed+=1
        self.overall.update(self.oid,completed=self.done)
        if self.ui.plain:self.ui.say(f'Item {self.done}/{self.total} · {status}')

    def __rich_console__(self,console,options):
        compact=options.max_width<60
        percent=f'{min(100,max(0,self.downloaded/self.byte_total*100)):.0f}%' if self.byte_total else '—%'
        label=percent if compact else percent+' · '+size(self.downloaded)+(f'/{size(self.byte_total)}' if self.byte_total else '')
        self.file.update(self.fid,label=label)
        rate=size(self.speed)+'/s'
        if compact:rate+=' · '+size(self.downloaded)+(f'/{size(self.byte_total)}' if self.byte_total else '')
        if self.eta is not None:rate+=' · sisa '+duration(self.eta)
        status=self.message+(f' · sisa {duration(self.until-time.time())}' if self.until else '')
        yield Group(self.overall,self.file,Text(self.current,style=BLUE,no_wrap=True,overflow='ellipsis'),Text(rate,style=BLUE,no_wrap=True,overflow='ellipsis'),Text(f'Hasil  Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}',no_wrap=True,overflow='ellipsis'),Text('Status '+status,style=RED if 'GAGAL' in status else DIM,no_wrap=True,overflow='ellipsis'),Text('Durasi '+duration(time.monotonic()-self.started),style=DIM))

    def __exit__(self,exc_type,exc,tb):
        self.clock.active(False)
        if self.live:self.live.stop()
        label='Dihentikan' if exc_type in (KeyboardInterrupt,Cancelled) else 'Terhenti' if exc_type else 'Selesai'
        self.ui.say(label+f' · {self.done}/{self.total} item · {duration(time.monotonic()-self.started)}',BLUE)
        self.ui.say(f'Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}')
