# Status pemasangan YouTube Downloader 1.0.0

Selesai, 7 Oktober 2026.

- Tools aktif: `/home/aldebaran/tools/youtube-downloader`.
- Media: `/home/aldebaran/Videos/YouTube/{video,audio,subtitle}`. Folder ini masih kosong; percobaan disimpan di workspace.
- Lingkungan Python baru dibuat pada lokasi home, tanpa memakai venv X Bookmarks.
- Deno lokal 2.9.6 diverifikasi; semua dependensi sesuai lock, `pip check` dan `setup.sh --check` lulus.
- 23 tes paket lulus di workspace dan **23 tes yang sama lulus dari lokasi home** (36,757 detik).
- Status dan preview plain berhasil dijalankan dari home. Menu/panel warna dan Ctrl+C diuji pada pseudo-terminal; Warp belum diverifikasi langsung.
- File program home cocok dengan paket rilis. Pemeriksaan checksum file program X Bookmarks sebelum/sesudah pemasangan menunjukkan tidak ada perubahan.
- Percobaan YouTube nyata: video biasa dengan SRT/track subtitle, Short portrait 2160×2964, satu item playlist 144p dengan audio terbaik berhasil. Rincian di TEST-REPORT.md.
- Tidak ada versi YouTube Downloader terdahulu di home yang diganti; ini pemasangan pertama. Pembaruan berikutnya menggunakan update.sh dengan backup/rollback.

Mulai:

```bash
cd "$HOME/tools/youtube-downloader"
./youtube-downloader preview
./youtube-downloader 'LINK_YOUTUBE'
```

Program tidak otomatis mencari/memasang rilis baru. `setup.sh --upgrade` mengikuti versi yang dipatok; `update.sh` memasang paket program baru yang sudah diuji. MIGRASI.md menjelaskan pemindahan paket dan media/checkpoint.
