# YouTube Downloader 1.3.0

Tools terpisah dari X Bookmarks untuk video YouTube, Shorts, dan playlist. Rekomendasi utama adalah **kualitas maksimal + audio terbaik** yang tersedia dalam bahasa audio pilihan. Setiap pilihan resolusi juga mengambil pasangan video terbaik pada resolusi tersebut dan audio terbaik; 1440p bukan pasangan yang lebih rendah mutunya karena tidak berlabel rekomendasi.

## Pemasangan tiap OS

Python standar **3.10–3.14** didukung; gunakan **3.13** untuk pemasangan baru. Python free-threaded tidak termasuk dukungan. Target: Ubuntu 24.04+ x64, Windows 11 x64 native, macOS 15+ Intel/Apple Silicon. Linux ARM64 memiliki aset runtime tetapi belum diverifikasi. Windows ARM64, GUI, dan executable tanpa Python belum tersedia.

Ambil paket rilis atau clone repo privat `fathorrosi-dev/youtube-downloader` dengan akun yang diberi akses. Ekstrak seluruh paket ke folder tools; jangan hanya menyalin launcher. Folder boleh mengandung spasi dan Unicode. Setup membutuhkan internet untuk dependensi Python dan runtime Deno.

**Ubuntu**

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg
mkdir -p "$HOME/tools"
# Ekstrak paket ke ~/tools/youtube-downloader
cd "$HOME/tools/youtube-downloader"
bash setup.sh
bash setup.sh --check
./youtube-downloader preview
```

**macOS — Intel atau Apple Silicon**

Pasang [Homebrew](https://brew.sh), lalu:

```bash
brew install python@3.13 ffmpeg
cd "$HOME/tools/youtube-downloader"
export PATH="$(brew --prefix python@3.13)/libexec/bin:$PATH"
bash setup.sh
bash setup.sh --check
./youtube-downloader preview
```

Setup dan launcher memakai `python3` dari PATH. Untuk sesi terminal baru, jalankan ekspor PATH di atas lagi atau simpan barisnya di `~/.zprofile`. Jalankan setup kembali jika mengganti interpreter, OS, arsitektur, atau memindahkan folder. Pastikan Python native sesuai arsitektur yang digunakan; Intel memakai binary Deno Intel, Apple Silicon memakai ARM.

**Windows — PowerShell atau Command Prompt, tanpa WSL**

```powershell
winget install --exact --id Python.Python.3.13
winget install --exact --id Gyan.FFmpeg
```

Buka terminal baru supaya PATH terbaca. Jika `python --version` tidak menunjukkan Python yang dipasang, perbaiki PATH Python sebelum melanjutkan. Ekstrak paket misalnya ke `%USERPROFILE%\tools\youtube-downloader`, lalu dari PowerShell:

```powershell
cd "$HOME\tools\youtube-downloader"
.\setup.cmd
.\setup.cmd --check
.\youtube-downloader.cmd preview
.\youtube-downloader.cmd 'https://www.youtube.com/watch?v=ID_VIDEO'
```

Command Prompt memakai `cd /d "%USERPROFILE%\tools\youtube-downloader"`, `setup.cmd`, lalu `youtube-downloader.cmd "LINK"`. Launcher `.cmd` tidak membutuhkan perubahan execution policy. Pastikan `ffmpeg -version` dan `ffprobe -version` berhasil; setup memeriksa keduanya dan tidak mengunduh FFmpeg sendiri. Paket FFmpeg: [winget Gyan.FFmpeg](https://github.com/microsoft/winget-pkgs/tree/master/manifests/g/Gyan/FFmpeg), [formula Homebrew](https://formulae.brew.sh/formula/ffmpeg).

Pada contoh panduan berikut, Windows mengganti `./youtube-downloader` dengan `.\youtube-downloader.cmd`, `bash setup.sh` dengan `.\setup.cmd`, serta skrip pemeliharaan lain dengan padanan `.cmd`.

## Mulai dari awal

Setelah setup, jalankan dari folder tools. Contoh Ubuntu/macOS:

```bash
cd "$HOME/tools/youtube-downloader"
bash setup.sh --check
./youtube-downloader preview
./youtube-downloader 'https://www.youtube.com/watch?v=ID_VIDEO'
```

Atau jalankan `./youtube-downloader` lalu tempel link ketika diminta. URL Shorts dan playlist memakai perintah yang sama. Pakai tanda kutip pada link, terutama yang mengandung `&`.

Panah atas/bawah memilih, Enter mengonfirmasi, Space mencentang subtitle/item playlist. Ctrl+C membatalkan. Tidak ada download sebelum ringkasan terakhir disetujui. `--plain` mengganti menu dengan nomor dan download dengan teks singkat:

```bash
./youtube-downloader --plain 'https://www.youtube.com/shorts/ID_VIDEO'
```

Wizard memerlukan terminal untuk menjawab menu. Jika output dialihkan ke file, tampilan otomatis plain; stdin tetap perlu terminal. Job tersimpan bisa di-resume tanpa menu.

## Urutan pilihan

1. Tools mengambil judul, durasi, kanal, ukuran asli, dan format yang tersedia. Link video yang juga memuat playlist menawarkan video saja atau playlist.
2. Playlist menawarkan semua item, checkbox, atau nomor/rentang seperti `1,3,5-10`. Metadata tiap item diperiksa sebelum ringkasan. Video private/deleted dapat dilewati.
3. Pilih **Video + audio**, **Audio saja**, atau **Video saja — tanpa suara**.
4. Untuk video, pilih dulu **Format file**: *Otomatis* atau *MP4 kompatibel* (lihat bagian Hasil video + audio). Setelah itu video menawarkan kualitas maksimal, resolusi asli yang tersedia, atau pemilihan format detail per item. Tidak ada upscale. Pada playlist, target yang tidak tersedia turun ke resolusi terbaik di bawah target; perubahan terlihat dalam ringkasan. Item yang tidak memiliki resolusi di bawah target dilewati.
5. Pilih bahasa asli per video atau bahasa audio/dub yang benar-benar tersedia. Jika dub pilihan tidak ada pada item playlist, item itu dilewati; tools tidak mengganti bahasa diam-diam. `und` berarti YouTube tidak memberi identitas bahasa.
6. Audio saja menawarkan **audio asli terbaik** tanpa konversi atau **MP3** dengan konversi lossy. Bisa memilih format/bitrate secara detail.
7. Subtitle bersifat opsional. Pilih satu atau beberapa track, lalu tinjau ringkasan judul, kualitas, FPS, SDR/HDR, container, audio, perkiraan ukuran, dan tujuan file.
8. Pilih Mulai. ID job langsung disimpan agar proses dapat dilanjutkan.

Setiap jawaban tetap tertinggal di layar sebagai satu baris riwayat, misalnya `◆ 2/5 Kualitas  1080p`: simbol pink, nomor langkah redup, label biru, jawaban pink. Pilihan jenis unduhan tidak bernomor karena menentukan berapa langkah yang menyusul, sehingga setiap nomor yang tampil akurat. Ringkasan satu video berbentuk kartu (judul, kualitas, berkas, folder tujuan); playlist berbentuk tabel dengan judul yang dipotong dan diakhiri `…`. Dengan `--plain` simbol diganti ASCII (`[2/5] Kualitas: 1080p`).

Resolusi portrait dihitung dari sisi pendek: 1080×1920 ditampilkan sebagai 1080p. Video landscape maupun portrait mempertahankan orientasi, ukuran, FPS, dan codec sumber. Daftar dan estimasi bergantung pada format yang YouTube berikan saat pemeriksaan; ukuran yang belum diketahui tidak dibuat-buat.

## Hasil video + audio

**Hasil akhirnya satu video biasa dengan suara.** Video dan audio terpisah hanya dipakai sementara, kemudian FFmpeg menggabungkannya tanpa mengubah kualitas sumber. File sementara yang sukses dibersihkan. Video saja benar-benar tanpa audio, termasuk jika sumber format awal sudah memiliki audio.

### Format file: Otomatis atau MP4 kompatibel

**Otomatis** (bawaan) memilih video dan audio terbaik, lalu container sesuai codec: MP4 jika pasangan codec cocok, MKV untuk pasangan lain atau video dengan subtitle. YouTube biasanya menyimpan audio terbaik dalam Opus dan video resolusi tinggi dalam VP9 atau AV1, sehingga hasilnya sering MKV. MKV tetap satu file video dengan suara dan diputar baik di VLC dan mpv, tetapi QuickTime, iPhone, pemutar bawaan Windows, dan sebagian editor video sering tidak membukanya. Ringkasan sebelum mulai menampilkan container dan codec, misalnya `MKV · VP9 + Opus`.

**MP4 kompatibel** memilih video H.264 dan audio AAC tanpa re-encode, sehingga hasilnya MP4 untuk iPhone, Mac, Windows, dan editor video.

- YouTube tidak menyediakan H.264 di atas 1080p. Untuk 1440p dan 2160p tools memakai AV1, yang membutuhkan perangkat atau pemutar yang mendukungnya. Resolusi yang hanya ada dalam VP9 tidak ditawarkan pada mode ini.
- Audio AAC berkualitas sedikit di bawah Opus terbaik. Jika sebuah item tidak punya audio AAC untuk bahasa yang dipilih, item itu tetap diunduh sebagai MKV dan ringkasan memberi tahu jumlahnya.
- Subtitle tidak ditanam di MP4. Track tetap disimpan sebagai file `.srt` di folder `subtitle`.
- Pada pemilihan format detail, hanya video H.264/AV1 dan audio AAC yang ditampilkan (audio lain ditampilkan jika AAC tidak ada).
- Semua file MP4 diberi indeks di depan (`faststart`) agar cepat diputar, dan label bahasa audio ditulis tiga huruf (`eng`, `ind`) karena MP4 membuang kode dua huruf.

Pilihan ini tidak berlaku untuk audio saja. MP3 merupakan pilihan konversi terpisah, bukan default kualitas terbaik.

## Subtitle

Menu membedakan **CC asli**, **caption otomatis**, dan **terjemahan otomatis**, beserta bahasa sumber bila YouTube menyediakannya. Terjemahan hanya ditawarkan jika ada track/URL terjemahan dari YouTube. Tools tidak membuat terjemahan sendiri dan tidak mengubah suara menjadi dub baru.

Setiap track disimpan sebagai **SRT terpisah**. Untuk mode yang memiliki video, track juga dimasukkan ke MKV sebagai subtitle yang bisa dinyalakan/dimatikan; pada MP4 kompatibel track tidak ditanam dan hanya SRT terpisah yang ada. Untuk audio saja, SRT tetap terpisah. Tidak ada burn-in.

Subtitle gagal tidak membuang media yang sudah selesai: hasil ditandai partial. Resume mengambil kembali subtitle yang hilang dan memperbarui track dalam hasil akhir, menggunakan media yang sudah ada. Nama track membedakan CC dan caption otomatis. Pada playlist dengan banyak sumber, sumber dipilih per item dan terlihat pada ringkasan.

## Folder dan nama file

Default Ubuntu: `~/Videos/YouTube`; macOS: `~/Movies/YouTube`; Windows: folder Videos pengguna yang sebenarnya (termasuk jika dipindahkan), subfolder `YouTube`. `--output "FOLDER"` mengubah lokasi job baru dan tetap dihormati.

```text
ROOT_MEDIA/
├── video/       # video + audio atau video tanpa suara
├── audio/       # audio asli atau MP3
└── subtitle/    # SRT pilihan

~/tools/youtube-downloader/
├── youtube-downloader
├── settings.json       # opsional, pengaturan pengguna
├── .venv/              # dependensi terpisah
├── .runtime/bin/deno   # runtime lokal
└── .state/             # job, arsip, log, cache, file sementara, cadangan
```

Nama hasil memuat judul, ID YouTube, kualitas, mode, dan identitas varian. Berbeda resolusi, mode, bahasa audio, atau pilihan subtitle bisa menghasilkan file terpisah. Tidak ada folder per video/playlist. JSON/log/database tidak ikut masuk folder media.

Tujuan sekali pakai:

```bash
./youtube-downloader --output "$HOME/Videos/YouTubeLain" 'LINK_YOUTUBE'
```

Default dapat disimpan dalam `settings.json` saat tools berhenti:

```json
{
  "output": "~/Videos/YouTube",
  "browser": null
}
```

## Progress dan penghentian

Panel diperbarui di tempat yang sama, sekitar 12 kali per detik. Worker hanya melapor sekitar empat kali per detik; di antara dua laporan, bar file digeser sesuai kecepatan terakhir (paling lama 0,3 detik, dan tidak pernah mundur), sehingga geraknya mulus tanpa menambah lalu lintas event.

- Pink `#FFC2D4`: item playlist yang sudah ditangani, termasuk gagal/dilewati.
- Red `#E8384F`: file video atau audio aktif. Bar direset ketika berganti stream.
- Sky `#A8D8F0`: nama file, kecepatan, dan informasi pendukung.
- Blok `█` terisi, `░` kosong. Ukuran tidak diketahui memakai tiga blok bergerak dan tanpa persen/ETA; animasi berhenti saat menunggu/merge/gagal.
- Status menampilkan pemeriksaan, pengunduhan, retry, menunggu dengan countdown jika durasinya diketahui, dan penggabungan.
- Baris `Tahap` menunjukkan posisi item yang sedang berjalan, misalnya `✓ Metadata → ● Video → ○ Audio → ○ Gabung`: ✓ selesai, ● berjalan, ○ menunggu, ✗ tahap yang gagal. Subtitle muncul hanya jika dipilih; untuk video saja atau audio saja tahap terakhir bernama `Simpan`. Tahap yang dilewati (misalnya stream yang sudah ada saat resume) dihitung selesai. Pada terminal sempit hanya tahap yang berjalan yang diberi nama.
- Playlist (dua item atau lebih) menampilkan jendela 6 baris di bawah bar Item, dengan satu item selesai di atas item yang berjalan: ○ menunggu, ● berjalan, ✓ tersimpan, ! tersimpan tetapi subtitle belum lengkap, ✗ gagal, – dilewati (sudah ada di arsip). Baris disembunyikan pada terminal yang terlalu pendek untuk memuatnya bersama panel.

Pada terminal sempit, ukuran dipindah ke baris kecepatan. `NO_COLOR=1` mempertahankan blok tanpa warna. `--plain` tidak mencetak setiap pembaruan byte. Kegagalan selalu ditandai `! GAGAL`; merah pada bar bukan berarti gagal.

Ctrl+C menghentikan worker dan FFmpeg, memulihkan kursor, mencatat checkpoint, dan keluar dengan kode 130. Setelah selesai/dihentikan ada ringkasan permanen: tiap file dengan ukuran (✓ berhasil, ! subtitle belum lengkap, ✗ gagal beserta alasannya), folder hasil, ID job, dan lokasi log. Info teknis itu selalu tampil, tidak disembunyikan.

```bash
./youtube-downloader status
./youtube-downloader resume ID_JOB
```

File yang masih valid dilewati; varian lain tetap boleh diunduh. Jika hasil dihapus, resume mengunduhnya lagi. File parsial di `.state/temporary/` dipertahankan untuk percobaan berikutnya. Jika format pilihan sudah berubah/hilang di YouTube, buat job baru untuk meninjau pilihan baru. Resume mempertahankan pilihan job, bukan memilih kualitas lain otomatis.

## Login Firefox dan masalah koneksi

Download publik tidak memakai cookie secara default. Jika YouTube meminta login/konfirmasi umur/not-a-bot, batch berhenti supaya tidak meneruskan permintaan gagal. Login di Firefox, lalu jalankan:

```bash
./youtube-downloader --browser firefox 'LINK_YOUTUBE'
./youtube-downloader resume ID_JOB --browser firefox
```

Profil khusus: `--browser 'firefox:/jalur/profil'`. Tools memakai pembaca cookie yt-dlp, bukan meminta password atau membuat file cookie di media. Cookie browser tidak portable untuk komputer lain.

Retry jaringan dibatasi empat kali, timeout socket 30 detik. Tiga kegagalan sistem berturut-turut menghentikan batch. Subtitle menghormati `Retry-After` numerik dan retry dengan jeda. Batas permintaan YouTube/403 bisa tetap memerlukan menunggu dan sesi browser yang valid; jangan menganggap setiap penurunan speed sebagai Wi-Fi bermasalah.

Detail ada di `.state/download.log` dan `.state/setup.log`. Setiap log dipangkas saat mulai bila melebihi 5 MiB: isi lama dipindah ke `.log.1` (hanya satu salinan lama yang disimpan), sehingga log tidak tumbuh tanpa batas. Log/checkpoint dapat berisi URL bertanda tangan atau informasi sesi; jangan membagikannya mentah. Tools tidak menghapus hasil sukses ketika ada kegagalan.

## Setup dan pembaruan

Ubuntu memerlukan Python 3.10+ serta FFmpeg/FFprobe. Jika belum ada FFmpeg:

```bash
sudo apt update
sudo apt install ffmpeg python3 python3-venv
bash setup.sh
```

Setup membuat lingkungan Python lokal, memasang semua dependensi rilis meskipun yt-dlp global sudah ada (`pip install --require-hashes`: tiap file harus cocok dengan hash SHA-256 di `requirements.lock`, jadi paket yang diganti di PyPI ditolak), dan memasang Deno lokal dari distribusi resmi npm dengan verifikasi checksum SHA-512 yang dipatok (tanpa memerlukan Node/npm terpasang). Unduhan Deno yang terputus atau terpotong dilanjutkan otomatis; arsip yang checksum-nya tidak cocok dibuang dan diunduh ulang sekali dari awal sebelum setup dinyatakan gagal. Rilis 1.2.0 memakai yt-dlp 2026.8.19, EJS 0.8.0, Rich 14.3.4, Questionary 2.1.1, dan Deno 2.9.6; Python 3.10 memakai websockets 16.0 karena 17.2 mensyaratkan Python 3.11+; seluruh dependensi dipatok dalam `requirements.lock`. Ini tidak mengubah tools X Bookmarks.

```bash
bash setup.sh --check
bash setup.sh --upgrade
```

`--upgrade` **menyesuaikan dependensi dengan versi yang dipatok rilis terpasang**. Paket yang sudah sesuai biasanya dipertahankan; opsi ini tidak mencari yt-dlp terbaru atau memperbarui program.

Mulai 1.2.0, pembaruan program tersedia lewat satu perintah dari folder tools terpasang:

```bash
cd "$HOME/tools/youtube-downloader"
./youtube-downloader update --check  # periksa saja, tanpa memasang
./youtube-downloader update          # cari, unduh, verifikasi, lalu pasang
# jika sedang download, tunggu selesai:
./youtube-downloader update --wait
```

Windows: `.\youtube-downloader.cmd update --check` dan `.\youtube-downloader.cmd update`. macOS memakai perintah Ubuntu yang sama. Tambahkan `--plain` untuk keluaran teks. Tidak ada pemeriksaan otomatis saat download: pembaruan hanya berjalan saat lo memanggil `update`.

**Login GitHub sekali pada tiap perangkat.** Repo `fathorrosi-dev/youtube-downloader` privat; akun harus punya akses. Pasang GitHub CLI (`sudo apt install gh` di Ubuntu, `brew install gh` di macOS, `winget install --id GitHub.cli --exact` di Windows), lalu jalankan `gh auth login` dan pilih GitHub.com. Alternatif untuk otomasi: `GH_TOKEN` atau `GITHUB_TOKEN` dengan izin baca Contents repo. Jangan menuliskan token di argumen/perintah yang dibagikan; tools tidak menyimpan token dalam settings, paket, atau log.

Self-update mengambil **rilis stabil yang dipublikasikan setelah CI lintas OS lulus**, bukan isi branch pengembangan. ZIP dan SHA-256 diambil melalui API repo tetap, checksum diperiksa, dan metadata versi/OS/Python divalidasi sebelum installer dijalankan. Rilis sama/lebih lama dilewati; tidak ada downgrade otomatis. Checksum memeriksa integritas paket; sumber tepercaya tetap repo GitHub privat tersebut.

Pembaruan memakai kunci yang sama dengan download; jika tools aktif, berhenti dengan pesan. `--wait` menunggu. Program lama dicadangkan di `.state/program-backups/`; settings/job/database/media dipertahankan. Kegagalan pemasangan memicu rollback melalui installer. Detail pemasangan ada di `.state/update.log` dan `.state/setup.log`. Saat unduhan paket belum selesai, Ctrl+C membatalkan tanpa mengganti program; setelah tahap pemasangan mulai, Ctrl+C ditunda sampai transaksi selesai/pulih, lalu keluar 130.

Update manual tetap tersedia sebagai alternatif, termasuk pemasangan pertama dari versi 1.1.0 yang belum mengenal perintah `update`:

```bash
cd /jalur/paket-rilis-baru
bash update.sh "$HOME/tools/youtube-downloader"
```

Jika dependensi menu bermasalah, gunakan jalur stdlib `python3 self_update.py --check` atau `python3 self_update.py` dari folder terpasang (Windows: `python self_update.py`). Pemeriksaan OS/Python dan setup tetap berlaku. Jika rilis belum tersedia, login ditolak, atau koneksi putus, tools menjelaskan masalah; coba lagi setelah kondisi pulih.

```bash
cd "$HOME/tools/youtube-downloader"
bash rollback.sh
```

Rollback memulihkan program serta dependensi rilis sebelumnya tanpa menghapus media. Detail pemindahan ada di **MIGRASI.md**. Livestream aktif/premiere mendatang, seluruh kanal, dan burn-in belum termasuk cakupan tools ini.

Referensi mekanisme sumber: [yt-dlp](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp), [YouTube EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS), [Rich Live](https://rich.readthedocs.io/en/stable/live.html), [API rilis GitHub](https://docs.github.com/en/rest/releases/releases#get-the-latest-release), [aset rilis privat](https://docs.github.com/en/rest/releases/assets#get-a-release-asset).

## Pemeriksaan perangkat dan batas verifikasi

`status` menampilkan OS/arsitektur, Python, lokasi/version FFmpeg, FFprobe, dan Deno. `setup --check` memeriksa versi dependensi terhadap lock dan identitas environment. Jika pemeriksaan gagal, jalankan setup normal di perangkat tujuan.

Pengujian native Windows pada CI memakai **Windows Server 2025 x64**, bukan Windows 11. Pengujian macOS menggunakan runner macOS 15 Intel/Apple Silicon. Pemeriksaan langsung Windows 11 dan Warp merupakan verifikasi terpisah; lihat docs/TEST-REPORT.md (di repo, tidak ikut paket) untuk hasil yang benar-benar sudah berjalan.

Nama hasil mempertahankan judul Unicode yang dipendekkan, ID, kualitas, mode, dan identitas varian. Karakter nama file terlarang Windows diganti; benturan file tanpa arsip menghasilkan pesan gagal, bukan overwrite. Untuk jalur Windows yang terlalu panjang, gunakan folder tools/media yang lebih pendek atau aktifkan dukungan long paths Windows/Python.

Event/log memakai UTF-8; panel beralih ke plain ketika output dialihkan atau terminal tidak mendukung interaksi. `NO_COLOR` menonaktifkan warna. Ctrl+C menyimpan checkpoint, menghentikan worker dan turunannya, lalu mengembalikan 130. Kunci alat berlaku pula untuk setup/update/rollback/migrasi; `--wait` menunggu proses aktif selesai.
