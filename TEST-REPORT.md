# Verifikasi rilis 1.1.0

Status sementara: sedang menjalankan pemeriksaan native melalui GitHub Actions.

37 tes lulus di Ubuntu lokal, termasuk seluruh 23 tes regresi sebelumnya dan pemasangan bersih/migrate/update/rollback dengan dependensi sebenarnya. Tes tambahan memeriksa lock antarproses, NDJSON rusak/terlalu besar/tanpa laporan akhir, startup handshake, crash dan Ctrl+C dengan proses FFmpeg asli, nama Unicode, jalur antar-OS, dry-run/rebase berulang serta pemulihan transaksi.

CI memakai HTTP lokal + yt-dlp asli + FFmpeg/FFprobe asli + subtitle fixture. Matriks: ubuntu-24.04, windows-2025 (Windows Server x64), macos-15-intel, macos-15 (Apple Silicon); masing-masing Python 3.10, 3.13, 3.14. Python 3.13 menjalankan pemasangan bersih dan siklus migrate/update/rollback dengan dependensi sebenarnya. Windows memakai ConPTY/pywinpty 3.0.5 hanya untuk tes.

Windows 11 pengguna, Warp langsung, dan Ubuntu ARM64 belum diperiksa. Percobaan YouTube nyata terpisah dari CI dan bukan syarat commit. Hasil 1.0.0 tidak dianggap sebagai verifikasi native lintas OS 1.1.0.

Percobaan YouTube terbatas pada Ubuntu 7 Oktober 2026: video biasa 19 detik, 320×240 (kualitas tertinggi sumber), audio dan satu track SRT/embedded berhasil; Short portrait 2160×2964 dengan audio berhasil. Metadata playlist berhasil; download playlist dibatasi durasi 60 detik sehingga item pertama yang lebih panjang tidak diunduh. Bukti rinci privat tersimpan di `.state/live-smoke.json`, bukan repo.

Pemasangan sungguhan 1.0.0 → 1.1.0 → rollback 1.0.0 lulus pada Ubuntu lokal. Pin websockets 17.2 dipertahankan pada Python 3.11–3.14; Python 3.10 memakai 16.0 yang kompatibel. Ini penyesuaian yang diperlukan untuk memenuhi batas dukungan Python 3.10.
