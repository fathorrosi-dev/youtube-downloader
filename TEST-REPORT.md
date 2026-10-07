# Pengujian YouTube Downloader 1.0.0

Tanggal: 7 Oktober 2026, Ubuntu x86_64. Pengujian memakai Python 3.14.4, FFmpeg/FFprobe 8.0.1, yt-dlp 2026.8.19, EJS 0.8.0, Deno 2.9.6, Rich 14.3.4, Questionary 2.1.1.

## Paket lokal

**23 tes lulus**, termasuk yt-dlp asli yang mengunduh stream melalui server HTTP lokal dan FFmpeg asli yang merakit/FFprobe memeriksa hasil. Tidak hanya meniru pesan terminal.

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

- Pemilihan resolusi 720/1080/1440/2160/4320, pasangan audio, bahasa asli/dub, portrait, fallback turun saja, manual ID, audio asli/MP3, MP4/MKV.
- Identitas varian stabil terhadap perubahan estimasi dan target yang menghasilkan format sama; varian kualitas lain terpisah.
- Video + audio menjadi satu media, video saja tanpa suara, audio saja tanpa video; codec video H.264 tetap sama setelah merge, MP3 diuji sebagai konversi eksplisit.
- Dua SRT dimasukkan ke MKV sebagai dua track; subtitle yang hilang tidak menggagalkan media. Percobaan ulang memakai media lama tanpa event download media baru.
- Arsip mengenali file valid, file dihapus dapat diunduh kembali; benturan file tanpa arsip ditolak, tidak ditimpa.
- Worker mati tanpa laporan, auth/private/system error; breaker tiga kegagalan dan checkpoint interrupted/blocked; kunci yang sama untuk pemeliharaan.
- Menu panah/Enter dan checkbox Space benar-benar dijalankan pada pseudo-terminal, bukan hanya pemanggilan fungsi menu.
- Panel 7 baris pada lebar 40/60/100/160, blok/progres pecahan, reset/unknown/wait, palet RGB yang dipilih, NO_COLOR, plain, terminal redirect, output diperbarui di tempat dan kursor kembali setelah SIGINT/130.
- Pembaruan/rollback/migrasi melindungi settings/database, gagal update memulihkan program, migrasi meninggalkan sumber dan meminta environment baru.
- Callback retry memberikan waktu tunggu diketahui; penyelesaian file membawa byte final sehingga bar dan persen sama-sama 100%.
- Runtime dengan checksum salah ditolak sebelum binary dipasang/dijalankan.

Paket terpasang di home dan 23 tes juga lulus dari lokasi tersebut (36,757 detik). Status/preview home berhasil; folder media pengguna tetap kosong. Installer berhasil membangun environment nyata dan memeriksa semua versi serta `pip check`. Unduhan GitHub Deno 2.9.7 timeout; rilis memilih distribusi resmi npm **2.9.6**, dengan checksum SHA-512 aset x86_64/arm64 dipatok di `runtime.json`. Tidak menjalankan npm install script dan tidak memerlukan Node global. Binary nyata lolos pemeriksaan versi. Jalur arm64 tersedia namun belum diuji pada mesin arm64.

## YouTube nyata

Percobaan eksplisit menyimpan hasil di `.state/smoke-media` workspace, terpisah dari folder pengguna. Ringkasan mekanis ada di `.state/live-smoke.json` dan log rinci privat di `.state/download.log`.

- `jNQXAC9IVRw` (Me at the zoo): berhasil pada 320×240, satu file MKV berisi video, audio, dan satu subtitle. SRT juga tersimpan.
- Short `18NGQq7p3LY`: berhasil pada kualitas maksimal yang dikembalikan, portrait **2160×2964** dengan suara. Tidak ada konversi orientasi/ukuran.
- Playlist publik dari test suite upstream terbaca sebagai satu item (Big Buck Bunny, 635 detik). Satu item selesai diunduh pada **144p + audio terbaik**, lalu FFprobe memastikan satu MKV memiliki video dan audio. Uji online dibatasi satu item/resolusi rendah; pemilihan banyak item dan fallback diuji melalui simulasi lokal.
- Fixture lama `BaW_jenozKc` tidak tersedia. Error tersebut tidak dipakai sebagai bukti download sukses; klasifikasi video unavailable ditambahkan agar batch tidak memperlakukannya sebagai kerusakan sistem.

Terjemahan otomatis tersedia bergantung pada YouTube. Pemilihan sumber/target dan dua track sudah diuji lokal; tidak mengklaim setiap bahasa tersedia pada setiap video. Video login/age restricted tidak dicoba dengan cookie pengguna.

## Batas verifikasi

Warp tidak dapat dikontrol lewat akses aplikasi yang tersedia dalam sesi ini. Pengujian warna, menu, pembaruan panel, dan Ctrl+C memakai terminal interaktif/pseudo-terminal. Jalankan `./youtube-downloader preview` di Warp untuk memeriksa tampilannya pada font/ukuran terminal pribadi. Ini tidak memakai jaringan.

Tidak ada jaminan akses setiap video YouTube: perubahan format, auth, region, atau batas permintaan tetap dapat menghasilkan error. Tools menghentikan batch pada auth/tiga kegagalan sistem dan menyimpan checkpoint untuk penanganan berikutnya. Runtime/dependensi rilis dipatok; pembaruan program memerlukan paket baru yang diuji.
