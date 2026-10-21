# YouTube Downloader 1.3.1

Tools terpisah dari X Bookmarks untuk video YouTube, Shorts, dan playlist. Rekomendasi utama adalah **kualitas maksimal + audio terbaik** yang tersedia dalam bahasa audio pilihan. Setiap pilihan resolusi juga mengambil pasangan video terbaik pada resolusi tersebut dan audio terbaik; 1440p bukan pasangan yang lebih rendah mutunya karena tidak berlabel rekomendasi.

## Pemasangan tiap OS

Python standar **3.11–3.14** didukung; gunakan **3.13** untuk pemasangan baru. Python 3.10 sudah berakhir masa dukungnya (1 Oktober 2026) dan tidak lagi didukung. Python free-threaded tidak termasuk dukungan. Target: Ubuntu 24.04+ x64, Windows 11 x64 native, macOS 15+ Intel/Apple Silicon. Linux ARM64 memiliki aset runtime tetapi belum diverifikasi. Windows ARM64, GUI, dan executable tanpa Python belum tersedia.

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

Wizard memerlukan terminal untuk menjawab menu, kecuali semua jawabannya diberikan lewat flag dan `--yes` (bagian *Tanpa menu*). Seluruh tampilan ditulis ke **stderr**, jadi stderr yang menentukan bentuknya: plain bila stderr bukan terminal, `TERM=dumb`, atau `--plain`. Mengalihkan atau menangkap **stdout** tidak mengubah panel dan warna; stdout hanya berisi path hasil. Satu pengecualian di Windows: menu panah (prompt_toolkit) menggambar lewat konsol stdout, jadi bila stdout dialihkan menu tampil sebagai pilihan bernomor (ketik angkanya), sementara panel dan warna tetap. Ctrl+D (akhir input) pada pertanyaan sama dengan membatalkan: keluar 130 tanpa pesan galat. Job tersimpan bisa di-resume tanpa menu.

**Warna dan gerak.** Kedalaman warna mengikuti terminal: di Linux dan macOS dari `COLORTERM` (`truecolor`/`24bit`) dan `TERM`, di Windows dari konsolnya (24-bit pada Windows 10 build 15063 ke atas), lalu turun otomatis ke 256 atau 16 warna. Tiga warna membawa tampilan: pink untuk pilihan Anda dan keberhasilan, sky blue untuk proses yang sedang berjalan, merah hanya untuk kegagalan. Bar file memakai pink yang menghangat ke merah (scarlet) di ujungnya. Ada dua set warna, gelap dan terang, dengan kontras teks minimal 4,5:1 dan kontras bar minimal 3:1 terhadap latar masing-masing. Pengaturan lewat lingkungan:

| Variabel | Nilai | Fungsi |
| --- | --- | --- |
| `YTDL_THEME` | `auto` (bawaan), `dark`, `light` | Set warna. `auto` membaca `COLORFGBG`; tanpa itu dianggap gelap. |
| `YTDL_MOTION` | `auto` (bawaan), `off` | `off` mematikan semua animasi (spinner, denyut warna, banner yang diketik). Plain, `NO_COLOR`, dan keluaran non-terminal tidak pernah beranimasi. |
| `YTDL_INSPECT_WORKERS` | angka, bawaan 3 | Jumlah item playlist yang diperiksa bersamaan; `1` berarti satu per satu. |
| `YTDL_PACE` | `normal` (bawaan), `careful`, `fast` | Jeda yang disengaja agar YouTube tidak membatasi permintaan: acak sebelum tiap file yang diunduh, dan tetap antara permintaan satu pemeriksaan. `normal` (1-2 d sebelum tiap file, 1 d antar-permintaan) adalah yang selalu dipakai tools ini; `careful` 3-6 d dan 2 d; `fast` 0,5-1 d dan tanpa jeda antar-permintaan. `fast` bisa memicu pembatasan dari YouTube, jadi bukan bawaan. Nama lain ditolak sebelum ada yang ditanyakan, dengan daftar nama yang diterima. `status` menampilkan profil yang berlaku. |
| `NO_COLOR` | nilai apa pun | Tanpa warna, bentuk panel tetap. |

## Urutan pilihan

1. Tools mengambil judul, durasi, kanal, ukuran asli, dan format yang tersedia. Link video yang juga memuat playlist menawarkan video saja atau playlist.
2. Playlist menawarkan semua item, checkbox, atau nomor/rentang seperti `1,3,5-10`. Metadata tiap item diperiksa sebelum ringkasan, tiga item sekaligus; urutan hasil dan perilaku saat dihentikan tidak berubah dibanding satu per satu. Video private/deleted dapat dilewati.
3. Pilih **Video + audio**, **Audio saja**, atau **Video saja — tanpa suara**.
4. Untuk video, pilih dulu **Format file**: *Otomatis* atau *MP4 kompatibel* (lihat bagian Hasil video + audio). Setelah itu video menawarkan kualitas maksimal, resolusi asli yang tersedia, atau pemilihan format detail per item. Tidak ada upscale. Pada playlist, target yang tidak tersedia turun ke resolusi terbaik di bawah target; perubahan terlihat dalam ringkasan. Item yang tidak memiliki resolusi di bawah target dilewati.
5. Pilih bahasa asli per video atau bahasa audio/dub yang benar-benar tersedia. Jika dub pilihan tidak ada pada item playlist, item itu dilewati; tools tidak mengganti bahasa diam-diam. `und` berarti YouTube tidak memberi identitas bahasa.
6. Audio saja menawarkan **audio asli terbaik** tanpa konversi atau **MP3** dengan konversi lossy. Bisa memilih format/bitrate secara detail.
7. Subtitle bersifat opsional. Pilih satu atau beberapa track, lalu tinjau ringkasan judul, kualitas, FPS, SDR/HDR, container, audio, perkiraan ukuran, dan tujuan file.
8. Pilih **Mulai**, **Ubah pilihan**, atau Batal. *Ubah pilihan* kembali ke pertanyaan pertama dengan jawaban sebelumnya sudah terpilih; info video dan playlist tidak diambil ulang. Pertanyaan yang tadinya dijawab oleh flag (misalnya `--quality 1080`) ikut ditanyakan lagi, dengan jawaban dari flag itu sudah terpilih. Setelah Mulai, ID job langsung disimpan agar proses dapat dilanjutkan.

Setiap jawaban tetap tertinggal di layar sebagai satu baris riwayat, misalnya `◆ 2/5 Kualitas  1080p`: simbol pink, nomor langkah redup, label biru, jawaban pink. Pilihan jenis unduhan tidak bernomor karena menentukan berapa langkah yang menyusul, sehingga setiap nomor yang tampil akurat. Ringkasan satu video berbentuk kartu (judul, kualitas, berkas, folder tujuan); playlist berbentuk tabel dengan judul yang dipotong dan diakhiri `…`. Dengan `--plain` simbol diganti ASCII (`[2/5] Kualitas: 1080p`).

Resolusi portrait dihitung dari sisi pendek: 1080×1920 ditampilkan sebagai 1080p. Video landscape maupun portrait mempertahankan orientasi, ukuran, FPS, dan codec sumber. Daftar dan estimasi bergantung pada format yang YouTube berikan saat pemeriksaan; ukuran yang belum diketahui tidak dibuat-buat.

## Tanpa menu (skrip)

Setiap pertanyaan wizard punya flag. `--yes` menjawab pertanyaan yang tidak diberi flag dengan nilai bawaan, menyetujui ringkasan, dan satu-satunya yang membuat wizard berjalan tanpa terminal:

```bash
./youtube-downloader 'LINK' --yes --quality 1080 --format mp4 --subs en,id
./youtube-downloader 'LINK_PLAYLIST' --yes --items 1,3,5-10 --mode audio --audio-format mp3
berkas=$(./youtube-downloader 'LINK' --yes)   # path hasil, satu per baris
```

| Flag | Menjawab | Bawaan di bawah `--yes` |
| --- | --- | --- |
| `--mode video-audio\|audio\|video` | Unduh apa? | `video-audio` |
| `--format auto\|mp4` | Format file (`mp4` = MP4 kompatibel) | `auto` |
| `--quality best\|TINGGI` | Kualitas, misalnya `1080` | `best` |
| `--lang original\|KODE` | Bahasa audio | `original` |
| `--audio-format original\|mp3` | Format audio (audio saja) | `original` |
| `--subs en,id\|none` | Bahasa subtitle | tanpa subtitle |
| `--items 1,3,5-10\|all` | Item playlist | semua |
| `--playlist` | Link video+playlist: ambil playlist | video saja |

Beberapa link sekaligus: `--links-file berkas.txt` (satu link per baris; baris kosong dan baris berawalan `#` dilewati) hanya bersama `--yes`, menjalankan tiap link dengan flag yang sama dan menolak `--items`/`--playlist`:

```bash
./youtube-downloader --links-file link.txt --yes --quality 1080
```

Nilai yang tidak ditawarkan video itu ditolak dengan daftar pilihan; flag yang tidak berlaku untuk pilihan lain (misalnya `--quality` pada audio saja) ditolak, tidak diabaikan diam-diam. Untuk tiap bahasa di `--subs`, CC asli didahulukan dari caption otomatis dan terjemahan; bahasa yang tidak ada disebutkan. Ringkasan tetap dicetak sebelum unduhan.

Path file yang tersimpan (✓ dan ! subtitle belum lengkap) dicetak satu per baris ke stdout dalam mode `--yes`, dan juga pada wizard atau resume interaktif bila stdout ditangkap atau dialihkan; jika tidak, stdout kosong. Kode keluar: 0 selesai, 1 gagal atau ada yang belum selesai, 130 dihentikan.

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

- Pink (gelap `#FFC2D4`, terang `#C9205F`): item playlist yang sudah ditangani, jawaban Anda, dan keberhasilan.
- Sky blue (gelap `#A8D8F0`, terang `#0B6FA4`): bar item (`Item`), nama file, kecepatan, dan informasi pendukung; huruf tahap yang sedang berjalan berdenyut halus antara biru dan pink.
- Bar file (`File`): pink di pangkal (gelap `#FFC2D4`, terang `#CC3D7B`) yang menghangat menjadi scarlet di ujung (gelap `#FF4433`, terang `#D6361F`); ujung selalu bagian yang paling merah. Saat byte mengalir, seluruh isi bar "bernapas" ke arah merah dan kembali dalam 1,6 detik; saat menunggu, menggabungkan, atau gagal, bar diam dalam gradasi biasa. Scarlet ini sengaja berbeda dari merah kegagalan. Bar baru penuh merah di ujungnya setelah kira-kira 9 sel; sebelum itu sebagian besar masih pink. Terminal 16 warna tidak punya gradasi: bar pink polos dengan dua sel merah di ujung, tanpa denyut. Bar tanpa ukuran (blok tiga sel yang bergerak) berwarna pink-merah-pink. `YTDL_MOTION=off` menghentikan denyut tetapi gradasi tetap ada.
- Merah (gelap `#FF5470`, terang `#D1122F`): hanya untuk kegagalan (`✗`, `Gagal`, `! GAGAL`). Bar file memakai scarlet-nya sendiri, bukan merah ini.
- Blok `█` terisi, `░` kosong. Dengan gerak aktif, status memakai spinner braille dan cahaya yang menyapu teks; saat file selesai bar menyala sekali lalu tenang. Pemeriksaan metadata dan langkah lama lain memakai spinner yang sama, dan banner judul diketik satu kali pada awal sesi interaktif. Ukuran tidak diketahui memakai tiga blok bergerak dan tanpa persen/ETA; animasi berhenti saat menunggu/merge/gagal.
- Status menampilkan pemeriksaan, pengunduhan, retry, menunggu dengan countdown jika durasinya diketahui, dan penggabungan. Jeda sebelum tiap file (lihat `YTDL_PACE`) tampil sebagai `Menunggu · jeda antar-unduhan · sisa 1d`, bukan sebagai unduhan 0 B/s; `--plain` mencetaknya sebagai satu baris per jeda, jadi dua baris tambahan tiap video.
- Baris `Tahap` menunjukkan posisi item yang sedang berjalan, misalnya `✓ Metadata → ● Video → ○ Audio → ○ Gabung`: ✓ selesai, ● berjalan, ○ menunggu, ✗ tahap yang gagal. Subtitle muncul hanya jika dipilih; untuk video saja atau audio saja tahap terakhir bernama `Simpan`. Tahap yang dilewati (misalnya stream yang sudah ada saat resume) dihitung selesai. Pada terminal sempit hanya tahap yang berjalan yang diberi nama.
- Playlist (dua item atau lebih) menampilkan jendela 6 baris di bawah bar Item, dengan satu item selesai di atas item yang berjalan: ○ menunggu, ● berjalan, ✓ tersimpan, ! tersimpan tetapi subtitle belum lengkap, ✗ gagal, – dilewati (sudah ada di arsip). Baris disembunyikan pada terminal yang terlalu pendek untuk memuatnya bersama panel.

- Satu video tidak punya batch untuk dihitung, jadi panelnya lima baris: bar File, baris Tahap, nama file, kecepatan, dan `Status ... · berjalan 16d`. Bar Item, baris Hasil, dan baris Durasi hanya muncul untuk playlist. Ringkasan akhir satu video berbentuk kartu (judul; kualitas, codec, format, ukuran; nama file) tanpa hitungan 1/1.

Pada terminal sempit, ukuran dipindah ke baris kecepatan. `NO_COLOR` yang berisi nilai mempertahankan blok tanpa warna (nilai kosong dianggap tidak diatur). `--plain` tidak mencetak setiap pembaruan byte; sebagai gantinya satu baris progres tiap sepersepuluh file (tiap menit bila ukurannya tidak diketahui), sekitar sepuluh baris per stream, misalnya `  video 42% · 120.3 MB/290.1 MB · 12.4 MB/s`, agar log tetap menunjukkan bahwa unduhan hidup. Kegagalan selalu ditandai `! GAGAL`.

Ctrl+C menghentikan worker dan FFmpeg, memulihkan kursor, mencatat checkpoint, dan keluar dengan kode 130. Saat pemeriksaan playlist, worker yang sedang memeriksa ikut dihentikan (baris status berganti menjadi `Menghentikan · menutup 3 pencarian yang berjalan`), jadi Ctrl+C tidak menunggu pencarian yang lambat. Hal yang sama berlaku saat batch pemeriksaan berhenti karena login atau pembatasan. Setelah selesai/dihentikan ada ringkasan permanen: tiap file dengan ukuran (✓ berhasil, ! subtitle belum lengkap, ✗ gagal beserta alasannya), folder hasil, ID job, dan lokasi log. Info teknis itu selalu tampil, tidak disembunyikan.

```bash
./youtube-downloader status
./youtube-downloader status --json   # satu objek JSON di stdout untuk skrip
./youtube-downloader status --timings   # waktu tiap fase per item pada job terbaru
./youtube-downloader resume            # menu job yang belum selesai
./youtube-downloader resume --last     # yang terbaru, tanpa menu
./youtube-downloader resume ID_JOB
```

`resume` tanpa ID menampilkan job berstatus prepared, running, interrupted, blocked, atau partial, yang terbaru di atas, lengkap dengan jumlah item selesai dan waktu terakhir. Tanpa terminal, beri ID atau `--last`. `status` menampilkan 10 job terakhir (`--json` mencetak versi, platform, dependensi, biner, ukuran `.state`, dan job sebagai satu objek JSON ke stdout, tanpa tabel); file job yang rusak ditandai `tidak terbaca` dan tidak membuat `status` gagal. `status` juga menampilkan profil jeda (`YTDL_PACE`) yang berlaku. Setiap akhir yang belum selesai ditutup dengan perintah `resume` persis, siap disalin (dengan `--browser firefox` bila yang diminta adalah login).

**Ke mana waktunya pergi.** Setiap item yang diunduh mencatat di file jobnya (`items[].timings`, detik dengan jam monoton) lamanya tiap fase yang berjalan (`metadata`, `video`, `audio`, `subtitle`, `finish` untuk penggabungan dan pemeriksaan), `total` waktu worker, `waited` (bagian yang dihabiskan untuk jeda yang disengaja dan jeda sebelum percobaan ulang, dihitung dari lama yang diminta), `bytes` media yang diunduh, `speed` (rata-rata byte per detik di luar waktu menunggu; tidak ada bila belum diketahui) dan `wall`, waktu yang diukur aplikasi di sekeliling worker. Selisih `wall` dan `total` adalah biaya memulai proses (impor yt-dlp, penutupan), yaitu angka yang menentukan apakah satu worker untuk beberapa video layak dipertimbangkan. Fase yang tidak berjalan tidak ada, dan item yang dilewati karena sudah ada di arsip tidak punya catatan waktu; job lama tanpa catatan tetap valid. `status --timings` menampilkan tabel per item untuk job terbaru yang punya catatan, dan `status --json` memuat jumlahnya per job (`jobs[].timings`; dengan `--timings` juga `per_item`). Catatan ini tidak berisi tautan, nama file, atau data akun. Penutup sesi menambahkan satu baris redup, misalnya `Rata-rata 3.1 MB/s · menunggu 14d`, hanya bila unduhan pada sesi itu mencatat waktu.

File yang masih valid dilewati; varian lain tetap boleh diunduh. Jika hasil dihapus, resume mengunduhnya lagi. File parsial di `.state/temporary/` dipertahankan untuk percobaan berikutnya. Jika format pilihan sudah berubah/hilang di YouTube, buat job baru untuk meninjau pilihan baru. Resume mempertahankan pilihan job, bukan memilih kualitas lain otomatis.

## Login Firefox dan masalah koneksi

Download publik tidak memakai cookie secara default. Jika YouTube meminta login/konfirmasi umur/not-a-bot, batch berhenti supaya tidak meneruskan permintaan gagal. Login di Firefox, lalu jalankan:

```bash
./youtube-downloader --browser firefox 'LINK_YOUTUBE'
./youtube-downloader resume ID_JOB --browser firefox
```

Profil khusus: `--browser 'firefox:/jalur/profil'`. Tools memakai pembaca cookie yt-dlp, bukan meminta password atau membuat file cookie di media. Cookie browser tidak portable untuk komputer lain.

Retry jaringan dibatasi empat kali, timeout socket 30 detik. Permintaan login dan batas laju (HTTP 429) menghentikan batch seketika, karena meneruskan permintaan hanya memperburuknya. Tiga kegagalan berurutan jenis lain (format tidak tersedia, jaringan, atau sistem) juga menghentikannya; video private/dihapus dilewati tanpa dihitung. Bila yt-dlp yang terpasang dirilis lebih dari 45 hari lalu, pesan henti menyebut umurnya dan menyarankan `update --check` untuk melihat apakah ada rilis alat baru (`update` hanya memasang rilis alat ini, bukan yt-dlp terbaru). Subtitle menghormati `Retry-After` numerik dan retry dengan jeda. Batas permintaan YouTube/403 bisa tetap memerlukan menunggu dan sesi browser yang valid; jangan menganggap setiap penurunan speed sebagai Wi-Fi bermasalah.

Detail ada di `.state/download.log` dan `.state/setup.log`. Setiap log dipangkas saat mulai bila melebihi 5 MiB: isi lama dipindah ke `.log.1` (hanya satu salinan lama yang disimpan), sehingga log tidak tumbuh tanpa batas. Log/checkpoint dapat berisi URL bertanda tangan atau informasi sesi; jangan membagikannya mentah. Tools tidak menghapus hasil sukses ketika ada kegagalan.

## Setup dan pembaruan

Ubuntu memerlukan Python 3.11+ serta FFmpeg/FFprobe. Jika belum ada FFmpeg:

```bash
sudo apt update
sudo apt install ffmpeg python3 python3-venv
bash setup.sh
```

Setup membuat lingkungan Python lokal, memasang semua dependensi rilis meskipun yt-dlp global sudah ada (`pip install --require-hashes`: tiap file harus cocok dengan hash SHA-256 di `requirements.lock`, jadi paket yang diganti di PyPI ditolak), dan memasang Deno lokal dari distribusi resmi npm dengan verifikasi checksum SHA-512 yang dipatok (tanpa memerlukan Node/npm terpasang). Unduhan Deno yang terputus atau terpotong dilanjutkan otomatis; arsip yang checksum-nya tidak cocok dibuang dan diunduh ulang sekali dari awal sebelum setup dinyatakan gagal. Rilis 1.3.1 memakai yt-dlp 2026.8.19, EJS 0.8.0, Rich 14.3.4, Questionary 2.1.1, websockets 17.2, dan Deno 2.9.6; seluruh dependensi dipatok dalam `requirements.lock`. Ini tidak mengubah tools X Bookmarks.

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

Windows: `.\youtube-downloader.cmd update --check` dan `.\youtube-downloader.cmd update`. macOS memakai perintah Ubuntu yang sama. Tambahkan `--plain` untuk keluaran teks. Tidak ada pemeriksaan otomatis saat download: pembaruan hanya berjalan saat Anda memanggil `update`.

**Login GitHub sekali pada tiap perangkat.** Repo `fathorrosi-dev/youtube-downloader` privat; akun harus punya akses. Pasang GitHub CLI (`sudo apt install gh` di Ubuntu, `brew install gh` di macOS, `winget install --id GitHub.cli --exact` di Windows), lalu jalankan `gh auth login` dan pilih GitHub.com. Alternatif untuk otomasi: `GH_TOKEN` atau `GITHUB_TOKEN` dengan izin baca Contents repo. Jangan menuliskan token di argumen/perintah yang dibagikan; tools tidak menyimpan token dalam settings, paket, atau log. Token yang berisi spasi atau baris baru di tengahnya ditolak dengan pesan yang menyebut asalnya (variabel lingkungan atau `gh auth token`); spasi di ujung dibuang.

Self-update mengambil **rilis stabil yang dipublikasikan setelah CI lintas OS lulus**, bukan isi branch pengembangan. ZIP dan SHA-256 diambil melalui API repo tetap, checksum diperiksa, dan metadata versi/OS/Python divalidasi sebelum installer dijalankan. Rilis sama/lebih lama dilewati; tidak ada downgrade otomatis. Checksum memeriksa integritas paket, bukan keaslian: siapa pun yang bisa mengubah rilis bisa mengubah keduanya. Karena itu rilis juga diberi *attestation* asal-usul (lihat README, bagian pengembang). Bila GitHub CLI tersedia, `update` memeriksanya dengan `gh attestation verify` yang dibatasi ke workflow rilis repo ini (`--signer-workflow`; `gh` lama yang belum mengenal opsinya hanya memeriksa repo, dan pesannya menyebut itu) dan melaporkan hasilnya; secara bawaan paket yang tidak terverifikasi tetap dipasang selama checksum cocok, karena repo privat tidak selalu mendapat fitur ini dari GitHub. Setel `YTDL_VERIFY_ATTESTATION=1` agar verifikasi **wajib**: update ditolak dan program tidak diubah bila attestation tidak dapat diperiksa. Hanya mode wajib itu yang melindungi dari rilis yang diubah.

Pembaruan memakai kunci yang sama dengan download; jika tools aktif, berhenti dengan pesan. `--wait` menunggu. Program lama dicadangkan di `.state/program-backups/`; settings/job/database/media dipertahankan. File program yang ada di rilis lama tetapi tidak lagi ada di rilis baru ikut dihapus (daftarnya dibaca dari `release.json`; rilis tanpa daftar tidak menghapus apa pun), dan tetap ada di cadangan. Kegagalan pemasangan memicu rollback melalui installer. Bila proses mati di tengah pemasangan, update berikutnya menolak dan menyuruh menjalankan `rollback`, yang memulihkan dari cadangan pemasangan yang terhenti itu. Installer menunggu hingga 5 detik bila kunci baru saja dilepas pemanggilnya. Detail pemasangan ada di `.state/update.log` dan `.state/setup.log`. Saat unduhan paket belum selesai, Ctrl+C membatalkan tanpa mengganti program; setelah tahap pemasangan mulai, Ctrl+C ditunda sampai transaksi selesai/pulih, lalu keluar 130.

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

## Membersihkan .state

`.state` menyimpan job, arsip, log, dan juga sisa yang tidak dihapus siapa pun: lingkungan Python lama setelah pindah folder, satu cadangan program per update, folder sementara job yang sudah selesai, cache pip/yt-dlp, dan cadangan jalur media. `status` menampilkan ukuran `.state` dan berapa yang dapat dibersihkan.

```bash
./youtube-downloader clean            # hanya daftar, tidak menghapus
./youtube-downloader clean --apply    # hapus
./youtube-downloader clean --days 7   # cadangan jalur media dianggap lama setelah 7 hari (bawaan 30)
./youtube-downloader clean --abandoned --days 30   # sertakan folder sementara job yang tidak selesai dan tidak disentuh 30 hari
```

Secara bawaan folder sementara job yang belum selesai tidak disentuh, karena `resume` memakainya. Dengan `--abandoned`, folder milik job yang tidak berstatus selesai dan tidak disentuh lebih dari `--days` hari (bawaan 30; dihitung dari pembaruan terakhir job) dicantumkan dengan ID job, status, umur, dan ukurannya, lalu `--apply` menghapusnya. Job tetap tersimpan; `resume` mengunduh ulang dari awal tanpa folder itu. Folder yang juga dibutuhkan job belum selesai yang lebih muda tidak ikut.

Yang dihapus: lingkungan Python lama, cadangan program kecuali yang terbaru dan target `rollback`, folder sementara milik job **yang sudah selesai** (dan, hanya dengan `--abandoned`, milik job yang ditinggalkan), cadangan jalur media yang lebih tua dari batas, cache, dan sisa proses yang terhenti lebih dari sehari. Tidak pernah disentuh: job, arsip, settings, kunci, log, dan media. Tautan simbolik tidak diikuti. `--apply` memakai kunci yang sama dengan download.

## Pemeriksaan perangkat dan batas verifikasi

`status` menampilkan OS/arsitektur, Python, ukuran `.state`, lokasi/version FFmpeg, FFprobe, dan Deno, serta versi tiap dependensi terhadap lock. Untuk yt-dlp ditambahkan umur rilisnya (versinya berbentuk tanggal), misalnya `2026.8.19 · rilis 57 hari lalu`, dengan catatan `sudah lama` setelah 45 hari dan saran `update --check`: unduhan YouTube lebih sering gagal bila yt-dlp lama. Tanpa jaringan, sehingga tidak tahu apakah yt-dlp yang lebih baru sudah ada; pemeriksaan itu dilakukan workflow mingguan di repo. `setup --check` memeriksa versi dependensi terhadap lock dan identitas environment. Jika pemeriksaan gagal, jalankan setup normal di perangkat tujuan.

Pengujian native Windows pada CI memakai **Windows Server 2025 x64**, bukan Windows 11. Pengujian macOS menggunakan runner macOS 15 Intel/Apple Silicon. Pemeriksaan langsung Windows 11 dan Warp merupakan verifikasi terpisah; lihat docs/TEST-REPORT.md (di repo, tidak ikut paket) untuk hasil yang benar-benar sudah berjalan.

Nama hasil mempertahankan judul Unicode yang dipendekkan, ID, kualitas, mode, dan identitas varian. Karakter nama file terlarang Windows diganti; file hasil yang sudah ada tanpa catatan arsip (misalnya proses berhenti tepat saat mencatat) diadopsi bila lolos pemeriksaan media, karena namanya memuat hash varian; file yang tidak lolos menghasilkan pesan gagal, bukan overwrite. Untuk jalur Windows yang terlalu panjang, gunakan folder tools/media yang lebih pendek atau aktifkan dukungan long paths Windows/Python.

Event/log memakai UTF-8; panel beralih ke plain ketika stderr (tempat seluruh tampilan ditulis) bukan terminal atau terminal tidak mendukung interaksi; stdout yang dialihkan tidak mengubahnya (di Windows hanya menu yang menjadi bernomor, lihat *Mulai dari awal*). `NO_COLOR` yang berisi nilai menonaktifkan warna. Ctrl+C menyimpan checkpoint, menghentikan worker dan turunannya, lalu mengembalikan 130. Kunci alat berlaku pula untuk setup/update/rollback/migrasi; `--wait` menunggu proses aktif selesai.
