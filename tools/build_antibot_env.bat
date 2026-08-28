@echo off
call "C:\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b 1
set "PATH=C:\Program Files\Go;C:\Program Files\LLVM\bin;C:\Users\Jason\nasm\nasm-2.16.03;C:\Users\Jason\Documents\AI\Signals\.venv\Scripts;C:\Users\Jason\.cargo\bin;%PATH%"
set "LIBCLANG_PATH=C:\Program Files\LLVM\bin"
set "VIRTUAL_ENV=C:\Users\Jason\Documents\AI\Signals\.venv"
set "CMAKE_GENERATOR=Ninja"
cd /d "C:\Users\Jason\Documents\AI\Signals\src\antibot\engine"
maturin develop --release
exit /b %errorlevel%
