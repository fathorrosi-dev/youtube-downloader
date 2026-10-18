# Migrasi, update, dan rollback YouTube Downloader 1.3.0

Program dan catatan berada dalam folder tools; media terpisah. Semua tindakan memakai `.state/run.lock`. Tunggu download selesai; `--wait` tersedia untuk pemeliharaan. Jangan menjalankan dua salinan tools bersamaan terhadap media yang sama.

## Pindah folder pada perangkat yang sama

Tujuan harus kosong di luar sumber. Migrasi menyalin program/settings/job/arsip/cadangan/file parsial, mengecualikan `.venv`, dan membuat ulang environment. Runtime hanya dipakai kembali jika metadata platform sesuai dan pemeriksaan versinya berhasil. Sumber tetap ada; media tetap di tempat semula.

```bash
cd /jalur/tools-lama
bash migrate.sh "$HOME/tools/youtube-downloader"
cd "$HOME/tools/youtube-downloader"
bash setup.sh --check
./youtube-downloader status
./youtube-downloader preview
```

Windows PowerShell: `.\migrate.cmd "$HOME\tools\youtube-downloader"`, lalu `.\setup.cmd --check`. Tujuan sudah berisi tools? Gunakan update dari paket rilis baru, bukan migrate. Setelah pindah, verifikasi satu resume sebelum menghapus sumber.

## Pindah perangkat atau OS

1. Tunggu proses selesai. Salin seluruh paket termasuk file tersembunyi, settings dan `.state`. Kecualikan `.venv`, `.runtime`, `__pycache__`, `.git`, serta `run.lock`. `.bootstrap`, lock dependensi, runtime/release metadata, launcher, dan program wajib ikut.
2. Salin **media terpisah** ke folder tujuan yang diinginkan. Pertahankan subfolder `video`, `audio`, `subtitle` dan nama file. Tidak ada konversi atau perubahan kualitas.
3. Pasang Python standar 3.11–3.14 dan FFmpeg/FFprobe sesuai PANDUAN.md. Jalankan setup pada perangkat tujuan untuk environment dan Deno OS/arsitektur yang benar. Jangan memakai environment/binary Linux di Windows/macOS.
4. Jalur dalam job dan arsip bersifat absolut. Bila akar media berubah, jalankan `rebase-media` di bawah sebelum resume.
5. Login ulang Firefox pada perangkat tujuan. Rebase mengosongkan pilihan profil lama dalam job/settings yang dimigrasikan. Jangan menyalin cookie/profil sebagai bagian rilis. Untuk sumber yang tidak membutuhkan login, browser tidak diperlukan; jika perlu, gunakan `--browser firefox` pada perangkat tujuan.

Default media: Ubuntu `~/Videos/YouTube`; macOS `~/Movies/YouTube`; Windows folder Videos pengguna/subfolder YouTube. Jika settings disalin, lokasi eksplisit lama tetap berlaku sampai dipetakan atau diubah untuk job baru. Resume menolak jalur asing dari OS lain sebelum download berjalan.

## Memetakan lokasi media tanpa download ulang

Aksi ini **tidak memindahkan atau menimpa media**. Salin file dahulu. `--from` memakai akar absolut lama persis, format Windows atau POSIX diterima pada semua OS. `--to` memakai folder lokal yang sudah ada.

Ubuntu/macOS, contoh dari Windows:

```bash
python3 maintenance.py rebase-media --from 'C:\Users\USER_LAMA\Videos\YouTube' --to "$HOME/Movies/YouTube" --dry-run
python3 maintenance.py rebase-media --from 'C:\Users\USER_LAMA\Videos\YouTube' --to "$HOME/Movies/YouTube"
```

Windows PowerShell, contoh dari Ubuntu:

```powershell
python maintenance.py rebase-media --from '/home/USER_LAMA/Videos/YouTube' --to "$HOME\Videos\YouTube" --dry-run
python maintenance.py rebase-media --from '/home/USER_LAMA/Videos/YouTube' --to "$HOME\Videos\YouTube"
.\youtube-downloader.cmd resume ID_JOB --browser firefox
```

Setelah `setup`, aksi yang sama tersedia dari launcher dengan flag yang sama, tanpa perlu mengetik `maintenance.py`: `./youtube-downloader rebase-media --from LAMA --to BARU [--dry-run]` (Windows: `.\youtube-downloader.cmd rebase-media ...`). Ia memakai kunci yang sama dengan download. `setup`, `migrate`, dan `rollback` tetap dijalankan lewat `.sh`/`.cmd`-nya, karena ketiganya mengubah lingkungan Python yang sedang dipakai perintah itu sendiri; `./youtube-downloader setup` menjelaskan hal ini dan menunjuk skrip yang benar.

Dry-run menampilkan jalur yang akan berubah, jumlah job/hasil, dan media/SRT yang belum tersedia. Benturan arsip atau nama ditolak tanpa perubahan. Penerapan memerlukan semua media/SRT tercatat yang dipetakan tersedia; bila file memang dihapus, buat job baru untuk pemulihan atau pulihkan file dahulu. Cadangan database/job/settings tersimpan di `.state/media-path-backups/`. Perubahan SQLite dan JSON dipulihkan bila penerapan gagal. Menjalankan ulang pemetaan yang sama tidak mengubah lagi catatan yang telah dipetakan.

Identitas varian dan schema arsip tetap sama; resume melewati hasil valid di lokasi baru. Jika ingin membatalkan pemetaan, hentikan proses lalu salin kembali database, settings, dan job dari cadangan yang tercetak; jangan mencampur cadangan dari tahap yang berbeda. Salinan media tetap di tempatnya.

## Self-update, update manual, dan rollback

Mulai 1.2.0, jalankan `./youtube-downloader update --check` untuk memeriksa atau `./youtube-downloader update --wait` untuk mengambil dan memasang rilis stabil terbaru. Windows memakai `.\youtube-downloader.cmd update --wait`. Di perangkat tujuan, pasang GitHub CLI lalu `gh auth login` dengan akun yang punya akses repo privat. Login perangkat lama tidak disalin oleh migrasi.

Self-update memverifikasi checksum dan metadata paket lalu memakai updater/backup/kunci yang sama. Tidak ada perubahan media dan tidak perlu rebase ulang untuk update di perangkat yang sama. Ctrl+C saat pemasangan ditunda hingga transaksi selesai, lalu keluar 130. Paket rilis tidak membawa settings atau kredensial pribadi. `setup --upgrade` hanya menyesuaikan dependensi dengan pin rilis terpasang.

Jika sumber masih 1.1.0, pemasangan pertama 1.2.0 memakai paket manual berikut; setelah itu perintah self-update tersedia.

Jalankan updater dari **paket rilis baru terpisah**, arahkan ke tools aktif:

```bash
cd /jalur/paket-rilis-baru
bash update.sh "$HOME/tools/youtube-downloader" --wait
cd "$HOME/tools/youtube-downloader"
bash setup.sh --check
./youtube-downloader status
```

Windows:

```powershell
cd 'C:\jalur\paket-rilis-baru'
.\update.cmd "$HOME\tools\youtube-downloader" --wait
cd "$HOME\tools\youtube-downloader"
.\setup.cmd --check
.\youtube-downloader.cmd status
```

Updater membuat cadangan file program di `.state/program-backups/`, mengganti program, lalu memeriksa dependensi. Gagal pemasangan memicu pemulihan program. Bila proses mati di tengah pemasangan (listrik padam, jendela ditutup paksa), update berikutnya berhenti dengan pesan; jalankan `rollback` untuk memulihkan program dari cadangan pemasangan yang terhenti itu, lalu ulangi update. File program yang ada di rilis lama tetapi tidak ada di rilis baru ikut dihapus dan tetap ada di cadangan, jadi rollback mengembalikannya. Settings, job, database, dan media tidak diganti oleh update/rollback.

```bash
bash rollback.sh
bash setup.sh --check
./youtube-downloader status
```

Windows padanannya `.\rollback.cmd`. Rollback terakhir dapat dijalankan satu kali. Rollback ke 1.1.0 mengembalikan program tanpa perintah self-update; untuk memasang kembali 1.2.0 gunakan paket manual. Cadangan **1.0.0 khusus Linux**: boleh dipulihkan di Linux, ditolak di Windows/macOS sebelum file diganti. Rollback tidak membatalkan media yang sukses atau pemetaan lokasi media. Cadangan program dari OS yang sama tetap bisa dipakai setelah migrate; riwayat cadangan lama lintas OS sebaiknya disimpan sebagai referensi, bukan dijalankan.

## Pemeriksaan setelah migrasi

Pastikan status menunjukkan lokasi program/media sesuai, `setup --check` lulus, dan preview tampil. Resume satu job dengan hasil yang sudah ada: harus dilewati tanpa download ulang. Coba satu hasil yang dihapus jika perlu pemulihan. File parsial tetap di `.state/temporary` untuk resume yt-dlp, tetapi keberhasilan melanjutkan byte bergantung pada server/sumber.

Paket selalu terpisah dari X Bookmarks. Folder Documents/Codex hanya salinan pengembangan; tools boleh dipasang permanen di home atau folder lokal pilihan.
