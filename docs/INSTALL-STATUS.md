# Pemasangan YouTube Downloader 1.2.0

> Catatan pemasangan ini merekam rilis 1.2.0. Sumber saat ini 1.3.0 dan belum dirilis; hasil verifikasinya ada di bagian "Perubahan setelah 1.2.0" pada [TEST-REPORT.md](TEST-REPORT.md).

Selesai, 7 Oktober 2026. Home `~/tools/youtube-downloader` sudah 1.2.0. Paket diunduh dari aset repo GitHub privat, checksum dan metadata diperiksa, lalu dipasang memakai installer terisolasi dan kunci alat. Cadangan 1.1.0: `.state/program-backups/1791378947876993719`; journal `.state/last-update.json` menunjuk cadangan tersebut.

- Seluruh 12 job CI native lulus; 52 tes termasuk regresi dan 15 tes self-update. Detail dan batas verifikasi ada di TEST-REPORT.md.
- Dependensi, kualitas, struktur media, dan identitas arsip dipertahankan. Home belum memiliki settings/job/arsip/media pengguna saat pembaruan ini; file pemeliharaan dan backup saja ditambahkan.
- Tidak ada perubahan pada tools X Bookmarks.
- Program memakai satu perintah `./youtube-downloader update`; `update --check` memeriksa tanpa memasang, `update --wait` menunggu kunci bebas. Login GitHub perangkat ini sudah tersedia melalui GitHub CLI.
- Panduan menjelaskan login tiap perangkat, rilis stabil, checksum, rollback, mode plain, serta Ctrl+C selama pemasangan.

```bash
cd "$HOME/tools/youtube-downloader"
./youtube-downloader update --check
./youtube-downloader update
```

## Riwayat pemasangan 1.1.0

Selesai, 7 Oktober 2026. Tools aktif di `~/tools/youtube-downloader`, diperbarui dari 1.0.0 menggunakan kunci proses dan updater. Cadangan 1.0.0 berada di `.state/program-backups/`; `.state/last-update.json` menunjuk cadangan terakhir untuk rollback.

- Versi terpasang 1.1.0; Python standar 3.14.4 pada Ubuntu perangkat ini.
- `setup.sh --check`, status, preview, dan 37 tes paket dari lokasi home lulus; tes pemasangan bersih dilewati pada pengujian home, karena sudah diuji terpisah dan pada CI Python 3.13. Artinya 36 tes benar-benar dijalankan dari home.
- Seluruh 12 kombinasi CI native lulus. Detail per platform di TEST-REPORT.md.
- Program home cocok dengan source setelah pemasangan. Data yang sudah ada dipertahankan; home belum memiliki job/arsip/media pengguna saat update ini dilakukan. Catatan pemeliharaan dan backup dibuat oleh updater.
- Checksum program X Bookmarks sebelum/sesudah sama; tools tersebut tidak diubah.
- Kualitas, pasangan audio, merge, nama/identitas varian, dan struktur media dipertahankan. Tidak perlu menjalankan migrasi media untuk pembaruan home ini.
- Windows 11 pengguna, aplikasi Warp langsung, dan Ubuntu ARM64 belum diverifikasi. Tes Windows native menggunakan Windows Server 2025 x64.

Mulai dari home:

```bash
cd "$HOME/tools/youtube-downloader"
./youtube-downloader status
./youtube-downloader preview
./youtube-downloader 'LINK_YOUTUBE'
```

Repo privat: [fathorrosi-dev/youtube-downloader](https://github.com/fathorrosi-dev/youtube-downloader). Repo/paket rilis hanya program, dokumen, dan tes; tidak menyertakan settings pribadi, cookie, job, database, media, `.venv`, atau `.runtime`. Pada versi 1.1.0 update program masih manual; mulai 1.2.0 perintah self-update tersedia. Setup --upgrade tetap mengikuti pin dependensi rilis.

PANDUAN.md menjelaskan pemasangan tiap OS; MIGRASI.md menjelaskan pindah perangkat, rebase-media, serta update/rollback.
