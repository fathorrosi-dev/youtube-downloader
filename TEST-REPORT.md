# Verifikasi rilis 1.2.0

Pengujian self-update sedang berjalan. Ditambahkan 15 tes protokol rilis stabil, autentikasi tanpa penyimpanan token, redirect tanpa kebocoran Authorization, checksum, validasi ZIP/versi/OS/Python, lock download, transaksi installer nyata dan rollback gagal, serta Ctrl+C yang ditunda sampai pemasangan selesai. Suite sekarang 52 tes termasuk 37 tes regresi.

CI native 1.2.0 dan pemasangan home belum ditandai selesai. Paket tidak dipublikasikan sebagai rilis stabil sebelum semua job native lulus. Riwayat rilis sebelumnya:

## Riwayat verifikasi 1.1.0

Selesai, 7 Oktober 2026. [Seluruh 12 job CI lulus](https://github.com/fathorrosi-dev/youtube-downloader/actions/runs/37616777106), revisi kode `c77434f`. Perubahan berikutnya hanya dokumentasi.

| Lingkungan native | Python 3.10 | Python 3.13 | Python 3.14 |
|---|---|---|---|
| Ubuntu 24.04 x64 | Lulus | Lulus | Lulus |
| Windows Server 2025 x64 | Lulus | Lulus | Lulus |
| macOS 15 Intel | Lulus | Lulus | Lulus |
| macOS 15 Apple Silicon | Lulus | Lulus | Lulus |

Paket memiliki 37 tes. Pada setiap kombinasi, 36 tes fungsional dijalankan; satu tes pemasangan bersih/siklus pemeliharaan diaktifkan pada Python 3.13 sehingga seluruh 37 tes dijalankan di keempat OS pada versi tersebut. Tes pemasangan memeriksa launcher asli, argumen/kode keluar, folder spasi/Unicode, setup/check, migrate/update/rollback, dan settings/job/arsip/media yang dipertahankan.

Tes proses memakai FFmpeg asli dan CLI yang benar-benar dihentikan saat download/merge; checkpoint harus berstatus interrupted dan proses turunannya harus sudah berhenti. Windows memakai ConPTY secara eksplisit. RGB, blok/animasi, menu panah/Space/Enter, NO_COLOR, plain, redirection, cursor restore, serta render 40/60/100/160 kolom lulus.

Batas verifikasi: Windows 11 pengguna, aplikasi Warp langsung, dan Ubuntu ARM64 belum diuji; hasil Windows native di atas berlaku untuk Windows Server 2025 pada CI.

37 tes lulus di Ubuntu lokal, termasuk seluruh 23 tes regresi sebelumnya dan pemasangan bersih/migrate/update/rollback dengan dependensi sebenarnya. Tes tambahan memeriksa lock antarproses, NDJSON rusak/terlalu besar/tanpa laporan akhir, startup handshake, crash dan Ctrl+C dengan proses FFmpeg asli, nama Unicode, jalur antar-OS, dry-run/rebase berulang serta pemulihan transaksi.

CI memakai HTTP lokal + yt-dlp asli + FFmpeg/FFprobe asli + subtitle fixture. Matriks: ubuntu-24.04, windows-2025 (Windows Server x64), macos-15-intel, macos-15 (Apple Silicon); masing-masing Python 3.10, 3.13, 3.14. Python 3.13 menjalankan pemasangan bersih dan siklus migrate/update/rollback dengan dependensi sebenarnya. Windows memakai ConPTY/pywinpty 3.0.5 hanya untuk tes.

Windows 11 pengguna, Warp langsung, dan Ubuntu ARM64 belum diperiksa. Percobaan YouTube nyata terpisah dari CI dan bukan syarat commit. Hasil 1.0.0 tidak dianggap sebagai verifikasi native lintas OS 1.1.0.

Percobaan YouTube terbatas pada Ubuntu 7 Oktober 2026: video biasa 19 detik, 320×240 (kualitas tertinggi sumber), audio dan satu track SRT/embedded berhasil; Short portrait 2160×2964 dengan audio berhasil. Metadata playlist berhasil; download playlist dibatasi durasi 60 detik sehingga item pertama yang lebih panjang tidak diunduh. Bukti rinci privat tersimpan di `.state/live-smoke.json`, bukan repo.

Pemasangan sungguhan 1.0.0 → 1.1.0 → rollback 1.0.0 lulus pada Ubuntu lokal. Pin websockets 17.2 dipertahankan pada Python 3.11–3.14; Python 3.10 memakai 16.0 yang kompatibel. Ini penyesuaian yang diperlukan untuk memenuhi batas dukungan Python 3.10.

Pemasangan Ubuntu ke home selesai setelah CI lulus, dengan backup program 1.0.0. Pemeriksaan setup/status dan 37 tes paket dari lokasi home berhasil (36 dijalankan + satu installer skip); checksum program source/home cocok dan X Bookmarks tidak berubah. Tidak ada job/arsip/media pengguna di home saat pembaruan.
