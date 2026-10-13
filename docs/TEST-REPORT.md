# Verifikasi rilis 1.2.0

Selesai, 7 Oktober 2026. [Seluruh 12 job native lulus](https://github.com/fathorrosi-dev/youtube-downloader/actions/runs/37626359655), revisi kode `e383c9e`. Perubahan sesudahnya hanya dokumentasi.

| Lingkungan native | Python 3.10 | Python 3.13 | Python 3.14 |
|---|---|---|---|
| Ubuntu 24.04 x64 | Lulus | Lulus | Lulus |
| Windows Server 2025 x64 | Lulus | Lulus | Lulus |
| macOS 15 Intel | Lulus | Lulus | Lulus |
| macOS 15 Apple Silicon | Lulus | Lulus | Lulus |

Suite memiliki 52 tes: 37 regresi + 15 self-update. Python 3.13 menjalankan seluruh 52 termasuk installer bersih dengan dependensi nyata; kombinasi lain menjalankan 51 dan melewati satu installer tersebut. Seluruh 52 tes lokal juga lulus; perbaikan metadata platform fixture diperiksa kembali lewat 15 tes lokal dan seluruh matriks CI.

Tes baru mencakup rilis stabil/equal/downgrade, autentikasi tanpa penyimpanan token, redirect tanpa meneruskan Authorization ke CDN, checksum dan nama aset, validasi ZIP/versi/OS/Python, lock download, cleanup paket sementara, installer transaksi nyata, pemulihan setelah gagal, serta Ctrl+C yang ditunda sampai transaksi selesai dan mengembalikan 130. Regresi download/menu/progres/NO_COLOR/plain/Ctrl+C worker tetap lulus.

Pengambilan aset dari repo GitHub privat sungguhan lulus: autentikasi, unduhan ZIP/SHA-256, redirect, dan ekstraksi aman. Paket CI yang sama dipasang dari aset privat ke home 1.1.0 → 1.2.0 dengan installer terisolasi dan backup 1.1.0. Tes HTTP/YouTube/FFmpeg sebelumnya tetap berlaku; perubahan ini tidak mengubah kualitas atau pemilihan media.

Windows 11 pengguna, Warp langsung, dan Ubuntu ARM64 belum diperiksa. Verifikasi Windows native berlaku untuk Windows Server 2025 CI. Publikasi melalui job tag hanya dapat berlangsung setelah seluruh job native lulus; publikasi pertama dilakukan manual sesudah CI yang sama berhasil.

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

## Perubahan setelah 1.2.0 (belum dirilis)

Belum diverifikasi oleh 12 job native CI; hasil di bawah hanya dari Ubuntu 24.04, Python 3.13.16, FFmpeg 6.1.1.

- Seluruh kode diformat ulang dengan ruff (perilaku tidak berubah), CI memperoleh job lint, dan `release` kini menunggu `lint` serta `native`.
- Tampilan CLI baru (simbol, step counter, riwayat jawaban, kartu hasil), rotasi log, batas event worker 8 MiB, `packaging` menggantikan `pip._vendor`, `requirements.lock` ber-hash dengan `pip install --require-hashes`, dan dokumen status dipindah ke `docs/`.
- Pilihan **Format file** di wizard (Otomatis atau MP4 kompatibel: H.264/AV1 + AAC tanpa re-encode, subtitle tetap `.srt` terpisah), ringkasan yang menampilkan codec, `faststart` dan label bahasa audio tiga huruf untuk semua MP4. Perilaku dan identitas unduhan mode Otomatis tidak berubah. Dicoba dengan FFmpeg asli (H.264 + AAC dari fixture); videonya bukan unduhan YouTube nyata.
- Tes pembersihan folder sementara di Windows kini mencoba ulang (`tests/fsutil.py`) dan `test_maximum_event_size` menulis `MAX_EVENT + 1` byte, setelah job Windows Python 3.13 gagal dengan `WinError 32`.
- 83 tes lulus (1 dilewati tanpa `YT_TEST_INSTALL=1`; 70 sebelum dua perubahan di atas). Tes pemasangan bersih dijalankan terpisah dengan `YT_TEST_INSTALL=1`: setup, check, preview, migrasi, update, dan rollback lulus memakai lock ber-hash.
- Tampilan wizard diperiksa pada terminal sungguhan (PTY dengan emulator layar) untuk video tunggal, playlist dengan satu item gagal, mode audio, `NO_COLOR` pada lebar 40, `--plain`, serta lebar 60 dan 100 kolom, dengan downloader tiruan. Pengunduhan YouTube nyata tidak dijalankan ulang.
- Belum diperiksa: Windows ConPTY dan macOS untuk tampilan baru, serta attestation rilis (lihat catatan di panduan pengembang).
