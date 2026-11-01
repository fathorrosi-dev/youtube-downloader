# YouTube Downloader 1.3.1

CLI Python untuk video YouTube, Shorts, dan playlist: pilihan resolusi asli + audio terbaik, audio saja, video saja, subtitle dan terjemahan yang tersedia (juga subtitle saja, tanpa mengunduh media). Video + audio digabung menjadi satu file tanpa re-encode (MKV atau MP4; pilihan **MP4 kompatibel** memakai H.264/AV1 + AAC untuk iPhone, Mac, dan editor video); MP3 merupakan opsi konversi terpisah.

- Ubuntu 24.04+ x64, Windows 11 x64 native, macOS 15+ Intel/Apple Silicon.
- Python standar 3.11–3.14; 3.13 direkomendasikan (3.10 sudah EOL sejak 1 Oktober 2026).
- FFmpeg/FFprobe dari PATH, Deno 2.9.6 lokal dengan checksum dipatok.
- Panel progres blok padat yang bergerak halus (12 fps) dengan animasi (spinner, denyut warna, banner), tiga warna (pink, sky blue, merah untuk kegagalan) dalam set gelap dan terang, bar item sky blue dan bar file pink yang menghangat ke scarlet, tahap tiap video, daftar item playlist, menu panah/Space/Enter, mode plain, `NO_COLOR`, `YTDL_THEME`, dan `YTDL_MOTION=off`.
- Resume (`resume` memilih job, `resume --last`), arsip varian, self-update rilis stabil dengan backup/rollback, migrasi jalur lintas OS.
- Mode skrip: semua pertanyaan wizard punya flag, `--yes` berjalan tanpa terminal, `--links-file` untuk banyak link, path hasil ke stdout, `status --json`.
- `status` menampilkan umur yt-dlp dan ukuran `.state`; `clean` membersihkan sisa dengan aman, `clean --abandoned` juga folder sementara job yang ditinggalkan.
- Jeda antar-unduhan tampil sebagai hitungan mundur "Menunggu" dan bisa dipilih dengan `YTDL_PACE` (`normal`, `careful`, `fast`); waktu tiap fase per item dicatat di job dan dilihat dengan `status --timings`.

Baca [PANDUAN.md](PANDUAN.md) dan [MIGRASI.md](MIGRASI.md). Laporan pengujian ([docs/TEST-REPORT.md](docs/TEST-REPORT.md)), catatan pemasangan, dan pratinjau berada di folder `docs/` pada repo; folder itu tidak ikut paket rilis. Paket ini tidak berisi settings pribadi, cookie, job, database, media, environment Python, atau binary runtime terpasang.

Ubuntu/macOS: `bash setup.sh`, lalu `./youtube-downloader 'LINK'`.
Windows: `setup.cmd`, lalu `youtube-downloader.cmd "LINK"`.

CI memakai runner OS native dan media HTTP lokal. Windows Server CI merupakan lingkungan uji tersendiri; pengujian langsung Windows 11/Warp dan Linux ARM64 belum tersedia. Self-update dijalankan saat diminta: `./youtube-downloader update` (Windows: `youtube-downloader.cmd update`). Repo privat membutuhkan GitHub CLI dengan login yang punya akses. `update --check` memeriksa tanpa memasang; tidak ada update diam-diam saat download.

## Menyiapkan rilis baru (pengembang)

Naikkan VERSION di model.py dan metadata release.json bersama dokumentasi. Jalankan tes lokal lalu push perubahan. Tag stabil berbentuk vX.Y.Z harus sesuai versi program. CI pada push tag menjalankan seluruh 12 job native; job publish hanya berjalan setelah semuanya lulus, membangun ZIP program-only + SHA-256 dan mengunggah GitHub Release. Self-update mengambil latest release stabil dari repo ini; commit di main saja tidak membuat pembaruan tersedia. Jangan memasukkan settings/cookie/state/media ke paket.

Job release juga membuat *attestation* asal-usul untuk ZIP (`actions/attest-build-provenance`). Repo privat membutuhkan paket GitHub yang menyediakannya; bila tidak ada, langkah itu gagal tanpa menghentikan rilis (`continue-on-error`). Saat `update`, bila GitHub CLI tersedia, attestation diperiksa dengan `gh attestation verify` (dibatasi ke workflow `compatibility.yml` bila `gh` mendukung `--signer-workflow`) dan hasilnya dilaporkan; `YTDL_VERIFY_ATTESTATION=1` mewajibkannya (update ditolak bila tidak terverifikasi). Tanpa mode wajib itu pemeriksaan hanya melaporkan dan tidak melindungi dari rilis yang diubah.

Workflow `dependencies.yml` berjalan tiap Senin: `scripts/lock_hashes.py --check`, `pip-audit` terhadap `requirements.lock`, dan `scripts/pin_status.py --check` (peringatan hanya bila pin yt-dlp lebih tua dari 45 hari **dan** PyPI punya rilis lebih baru). Temuan membuka satu issue; tidak ada yang berubah pada program. Workflow `bump-ytdlp.yml` (Senin, dan manual) menyiapkan cabang `bump/yt-dlp-VERSI` dan pull request yang hanya mengubah pin, versi EJS, dan hash; pengaturan repo *Allow GitHub Actions to create and approve pull requests* harus aktif agar PR terbuka otomatis (tanpa itu cabang tetap didorong dan run menyebutkannya). Run kompatibilitas dipicu eksplisit pada cabang itu; Anda yang menggabungkan dan membuat tag setelah 12 job lulus.

Dependensi dikunci dengan hash. Untuk mengganti versi, ubah hanya pin `==` di `requirements.lock` (dan `requirements.txt` untuk paket langsung), lalu jalankan `python scripts/lock_hashes.py` untuk menuliskan hash dari PyPI; `python scripts/lock_hashes.py --check` memeriksa bahwa hash di file masih sama dengan PyPI. `curl-cffi` (dengan `cffi` dan `pycparser`) dipatok di luar ekstra `[default]` karena subtitle YouTube hanya dilayani bila diminta dengan sidik jari TLS browser; versinya mengikuti set `pin-curl-cffi` milik rilis yt-dlp yang dipatok (yt-dlp mensyaratkan `curl-cffi>=0.5.10,<0.17`), jadi periksa ketiganya saat menaikkan pin yt-dlp. Folder `scripts/` hanya ada di repo, tidak ikut paket rilis.

Untuk menyiapkan paket lokal: `python3 release_bundle.py /jalur/output` (menulis daftar `files` ke `release.json` di dalam paket dan memverifikasinya; `python3 release_bundle.py --verify paket.zip` memeriksa paket yang sudah ada). Daftar itu dipakai `update` untuk menghapus file program yang tidak ada lagi di rilis baru. Publikasi manual juga wajib menunggu CI revisi kode yang sama lulus. Tidak perlu memperbarui pin dependensi kecuali perubahan tersebut sudah diuji sebagai bagian rilis baru.

## Gaya kode

CI menolak kode yang belum diformat. Sebelum commit: `python -m pip install ruff==0.17.0`, lalu `ruff format .` dan `ruff check .` (konfigurasi di `ruff.toml`). Di CI, `scripts/ci_summary.py` membungkus `unittest` sehingga tes yang gagal disebut namanya di anotasi dan ringkasan job. Di job Windows, `scripts/ci_ffmpeg.py` memasang FFmpeg lewat Chocolatey (diulang bila gagal; berhasil berarti `ffmpeg` dan `ffprobe` benar-benar ada, bukan kode keluar `choco`) dan, bila Chocolatey tetap tidak menjawab, dari rilis 9.0.2 yang hash SHA-256-nya dicek. Satu pernyataan per baris; titik koma pemisah dan pernyataan majemuk satu baris ditolak oleh lint.
