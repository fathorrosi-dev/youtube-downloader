# Migrasi dan rollback YouTube Downloader 1.0.0

Program, dependensi, dan catatan berada di folder tools. Media berada terpisah, default `~/Videos/YouTube`. Hentikan proses download sebelum memindahkan paket; mekanisme migrasi memakai kunci yang sama dan tidak menghentikan proses aktif.

## Pindah folder pada Ubuntu yang sama

Tujuan harus kosong dan berada di luar folder sumber. Migrasi menyalin paket beserta settings, job, database, cadangan, dan file parsial; sumber tetap ada. Lingkungan Python dibuat ulang supaya tidak membawa jalur absolut dari lokasi lama. Deno yang sudah terpasang dapat dipakai kembali setelah pemeriksaan versi/arsitektur.

```bash
cd /jalur/tools-lama
bash migrate.sh "$HOME/tools/youtube-downloader"
cd "$HOME/tools/youtube-downloader"
bash setup.sh --check
./youtube-downloader status
./youtube-downloader preview
```

Jika tujuan sudah berisi tools versi sebelumnya, gunakan `update.sh` dari paket rilis baru, bukan `migrate.sh`. Media tidak dipindahkan oleh `migrate.sh`; semua job lama tetap menunjuk ke lokasi hasil semula. Periksa status dan resume satu job sebelum menghapus paket sumber. Jangan menjalankan kedua salinan bersamaan: setelah dipisah, tiap folder mempunyai kunci dan database sendiri.

## Pindah komputer

1. Pastikan proses sudah berhenti. Salin **seluruh folder tools termasuk file tersembunyi**, kecuali `.venv`, `.runtime`, dan `__pycache__`. Simpan `.state`, `settings.json`, `.bootstrap`, program, requirements, dan dokumentasi. Jangan hanya menyalin launcher.
2. Salin seluruh `~/Videos/YouTube` secara terpisah, termasuk video/audio/SRT yang sudah selesai. Pertahankan nama file. Jika user dan lokasi hasil sama, checkpoint serta database tetap valid.
3. Pada Ubuntu tujuan pasang Python 3.10+ dan FFmpeg/FFprobe, lalu jalankan setup untuk membuat ulang dependensi dan Deno sesuai arsitektur:

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg
cd "$HOME/tools/youtube-downloader"
bash setup.sh
bash setup.sh --check
./youtube-downloader status
./youtube-downloader preview
```

4. Login ulang ke Firefox di komputer tujuan bila perlu. Profil/cookie komputer lama tidak dipindahkan otomatis.
5. Jika folder home/output berbeda, job dan database lama memiliki **jalur absolut**. Default output `~/Videos/YouTube` mengikuti home baru, tetapi catatan lama tetap menunjuk ke tempat lama. Untuk hanya download baru, ubah `settings.json` dan mulai wizard baru. Untuk meneruskan job lama tanpa download ulang, pindahkan media lalu rebasing catatan secara eksplisit seperti di bawah.

### Rebasing media ketika home berubah

Lakukan setelah menyalin media ke tujuan baru dan sebelum resume. Jalankan dari folder tools tujuan, ketika tidak ada proses lain. Ganti `OLD` dan `NEW` dengan akar media lama/baru yang persis. Script ini memakai kunci alat, membuat cadangan job/database/settings di `.state`, memeriksa file media tercatat di tujuan, dan hanya mengganti jalur yang berada di bawah `OLD`.

```bash
./.venv/bin/python - <<'PY'
from pathlib import Path
import json, shutil, sqlite3, time
from store import STATE, BASE, lock, write_json
OLD = Path('/home/USER_LAMA/Videos/YouTube').resolve()
NEW = Path.home() / 'Videos/YouTube'
NEW = NEW.resolve()

def remap(value):
    if isinstance(value, dict):
        return {k: remap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [remap(v) for v in value]
    if isinstance(value, str) and value.startswith('/'):
        p = Path(value)
        if p.is_relative_to(OLD):
            return str(NEW / p.relative_to(OLD))
    return value

with lock():
    assert OLD != NEW and NEW.is_dir(), 'Periksa OLD/NEW dan salin media dahulu'
    backup = STATE / ('media-paths-before-' + str(time.time_ns()))
    backup.mkdir()
    jobs = list((STATE / 'jobs').glob('*.json'))
    pending = [(p, remap(json.loads(p.read_text()))) for p in jobs]
    database = STATE / 'downloaded.sqlite3'
    rows = []
    if database.exists():
        with sqlite3.connect(database) as db:
            rows = [(rowid, remap(json.loads(payload))) for rowid, payload in db.execute('SELECT rowid,payload FROM results')]
            for rowid, result in rows:
                assert Path(result['path']).is_file(), 'Media tujuan belum lengkap: ' + result['path']
                for track in result.get('subtitles', []):
                    assert Path(track['path']).is_file(), 'SRT tujuan belum lengkap: ' + track['path']
            with sqlite3.connect(backup / 'downloaded.sqlite3') as saved:
                db.backup(saved)
            with db:
                db.executemany('UPDATE results SET path=?,payload=? WHERE rowid=?', [(result['path'], json.dumps(result), rowid) for rowid, result in rows])
    for p, job in pending:
        (backup / 'jobs').mkdir(exist_ok=True)
        shutil.copy2(p, backup / 'jobs' / p.name)
        write_json(p, job)
    settings = BASE / 'settings.json'
    if settings.exists():
        shutil.copy2(settings, backup / 'settings.json')
        write_json(settings, remap(json.loads(settings.read_text())))
    print('Jalur dipindahkan. Cadangan:', backup)
PY
```

Jika gagal sebelum pemeriksaan media selesai, tidak ada jalur yang diganti. Cadangan berada di `.state/media-paths-before-*`. Bila ada kegagalan setelah perubahan dimulai, pulihkan database/job/settings dari cadangan itu sebelum resume. Ini mengganti jalur catatan, bukan nama file/identitas varian atau isi media. File yang memang sudah dihapus boleh didownload ulang; untuk kondisi itu lebih sederhana mulai job baru.

## Pembaruan program

Gunakan paket rilis baru terpisah:

```bash
cd /jalur/paket-rilis-baru
bash update.sh "$HOME/tools/youtube-downloader"
```

Program lama dan manifest disimpan di `.state/program-backups/`. `.state/last-update.json` mencatat versi dan cadangan terakhir. Settings/job/media bukan bagian yang diganti. Dependensi paket disesuaikan dengan `requirements.lock`, bukan otomatis melompat ke yt-dlp terbaru. `--wait` menunggu kunci alat bebas.

```bash
cd "$HOME/tools/youtube-downloader"
bash rollback.sh
bash setup.sh --check
./youtube-downloader status
```

Rollback terakhir bisa dijalankan sekali; memulihkan program dan dependensi. Ia tidak membatalkan download sukses, mengembalikan media yang dihapus, ataupun mengubah lokasi media. Rilis berikutnya wajib menjaga pembacaan arsip dan struktur `video/audio/subtitle`; jika ada perubahan schema, panduan rilis harus menyediakan migrasinya sebelum update.

## Check setelah pindah

- Status menunjukkan folder tools dan media yang diharapkan.
- Preview memperlihatkan blok/palet yang sama; tidak menggunakan jaringan.
- Job dan database terbawa, media/SRT ada di lokasi tercatat.
- Resume melewati varian yang sudah valid; varian lain tetap dapat diunduh.
- File parsial tetap di `.state/temporary` agar yt-dlp dapat mencoba melanjutkan.

Download media tidak mengandalkan folder Documents/Codex. Paket di home dapat dipakai terus; workspace merupakan salinan pengembangan. Tool X Bookmarks tetap terpisah.
