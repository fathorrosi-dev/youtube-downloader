# YouTube Downloader 1.2.0

CLI Python untuk video YouTube, Shorts, dan playlist: pilihan resolusi asli + audio terbaik, audio saja, video saja, subtitle dan terjemahan yang tersedia. Video + audio digabung menjadi satu file tanpa re-encode; MP3 merupakan opsi konversi terpisah.

- Ubuntu 24.04+ x64, Windows 11 x64 native, macOS 15+ Intel/Apple Silicon.
- Python standar 3.10–3.14; 3.13 direkomendasikan.
- FFmpeg/FFprobe dari PATH, Deno 2.9.6 lokal dengan checksum dipatok.
- Panel progres blok padat, menu panah/Space/Enter, mode plain dan NO_COLOR.
- Resume, arsip varian, self-update rilis stabil dengan backup/rollback, migrasi jalur lintas OS.

Baca [PANDUAN.md](PANDUAN.md), [MIGRASI.md](MIGRASI.md), dan [TEST-REPORT.md](TEST-REPORT.md). Paket ini tidak berisi settings pribadi, cookie, job, database, media, environment Python, atau binary runtime terpasang.

Ubuntu/macOS: `bash setup.sh`, lalu `./youtube-downloader 'LINK'`.
Windows: `setup.cmd`, lalu `youtube-downloader.cmd "LINK"`.

CI memakai runner OS native dan media HTTP lokal. Windows Server CI merupakan lingkungan uji tersendiri; pengujian langsung Windows 11/Warp dan Linux ARM64 belum tersedia. Self-update dijalankan saat diminta: `./youtube-downloader update` (Windows: `youtube-downloader.cmd update`). Repo privat membutuhkan GitHub CLI dengan login yang punya akses. `update --check` memeriksa tanpa memasang; tidak ada update diam-diam saat download.
