# Rebuild dist\7seas-telemetrics.exe (run from this folder)
& ".\.venv\Scripts\python.exe" make_icon.py
& ".\.venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed `
    --name 7seas-telemetrics --icon icon.ico `
    --collect-all imageio_ffmpeg --add-data "icon.ico;." main.py
