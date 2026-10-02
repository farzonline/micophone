@echo off
rem Builds dist\Micophone.exe: one file, no console, no Python needed on the target PC.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller || exit /b 1
python test_receiver.py || exit /b 1
python theme.py || exit /b 1
python micophone_gui.py --smoke || exit /b 1
python -m PyInstaller --noconfirm --clean Micophone.spec
