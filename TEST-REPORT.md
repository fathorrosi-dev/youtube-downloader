# Verifikasi rilis 1.1.0

Status sementara: sedang menjalankan pemeriksaan native melalui GitHub Actions.

23 tes regresi versi sebelumnya lulus di Ubuntu. Tes tambahan memeriksa lock antarproses, NDJSON rusak/terlalu besar/tanpa laporan akhir, startup handshake, crash dan Ctrl+C dengan proses FFmpeg asli, nama Unicode, jalur antar-OS, dry-run/rebase berulang serta pemulihan transaksi.

CI memakai HTTP lokal + yt-dlp asli + FFmpeg/FFprobe asli + subtitle fixture. Matriks: ubuntu-24.04, windows-2025 (Windows Server x64), macos-15-intel, macos-15 (Apple Silicon); masing-masing Python 3.10, 3.13, 3.14. Python 3.13 menjalankan pemasangan bersih dan siklus migrate/update/rollback dengan dependensi sebenarnya. Windows memakai ConPTY/pywinpty 3.0.5 hanya untuk tes.

Windows 11 pengguna, Warp langsung, dan Ubuntu ARM64 belum diperiksa. Percobaan YouTube nyata terpisah dari CI dan bukan syarat commit. Hasil 1.0.0 tidak dianggap sebagai verifikasi native lintas OS 1.1.0.
