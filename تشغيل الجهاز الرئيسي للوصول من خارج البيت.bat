@echo off
chcp 65001 >nul
title تشغيل الجهاز الرئيسي - الوصول من خارج البيت
cd /d "%~dp0"

REM تحديد وضع التشغيل حسب النسخة المحملة بجانب هذا الملف
set MODE=
if exist "%~dp0نظام المحاسبة.exe" set MODE=EXE
if not defined MODE if exist "%~dp0app.py" set MODE=SOURCE
if not defined MODE (
  echo.
  echo لم أجد النسخة بجانب هذا الملف.
  echo ضع هذا الملف بجانب  نظام المحاسبة.exe  من النسخة المحمولة
  echo أو ضعه داخل مجلد  SmartAccounting  بجانب  app.py.
  echo.
  pause
  exit /b 1
)

if "%MODE%"=="EXE" (
  echo  الوضع المكتشف: نسخة برنامج جاهز  نظام المحاسبة.exe
) else (
  echo  الوضع المكتشف: الكود المصدري  app.py
)

echo.
echo   ============================================
echo    تشغيل النظام + تجهيز الوصول من خارج البيت
echo   ============================================
echo.

REM [1/4]
netsh advfirewall firewall show rule name="Accounting System" >nul 2>&1
if errorlevel 1 (
  echo [1/4] إضافة قاعدة جدار الحماية لمنفذ 5000...
  netsh advfirewall firewall add rule name="Accounting System" dir=in action=allow protocol=TCP localport=5000 >nul 2>&1
  echo        إن لم تُضف شغّل هذا الملف كمسؤول مرة واحدة.
  echo        قاعدة جدار الحماية جاهزة للمنفذ 5000.
) else (
  echo [1/4] قاعدة جدار الحماية موجودة مسبقاً.
)

echo [2/4] تشغيل النظام على المنفذ 5000...
if "%MODE%"=="EXE" (
  start "نظام المحاسبة" "%~dp0نظام المحاسبة.exe"
) else (
  start "نظام المحاسبة" /min pythonw.exe "%~dp0app.py"
)
timeout /t 4 /nobreak >nul
echo        النظام شغال - افتح المتصفح على العنوان أدناه.

echo [3/4] عناوين الشبكة المحلية لأجهزة البيت والمكتب:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R "IPv4"') do (
  for /f "tokens=*" %%i in ("%%a") do (
    set LOCALIP=%%i
    echo        - http://%%i:5000
  )
)

echo [4/4] العنوان العام الحالي للخارج:
for /f %%i in ('curl.exe -s --max-time 10 https://api.ipify.org') do set PUBLICIP=%%i
echo        - http://%PUBLICIP%:5000

REM حفظ العناوين
(
echo وضع التشغيل  : %MODE%
echo الرابط المحلي  : http://%LOCALIP%:5000
echo الرابط العام   : http://%PUBLICIP%:5000
echo.
echo ملاحظات:
echo - الرابط العام يتغير تلقائياً مع تغيير عنوان الـIP عند مزود الإنترنت.
echo - للينك ثابت دائم استخدم DDNS مثل duckdns.org
echo   راجع: دليل الربط من خارج البيت.txt
echo - لو الرابط العام مش بيفتح من الخارج فمزود الإنترنت بيستخدم CGNAT
echo   راجع دليل الربط لمعرفة البديل.
) > "%~dp0رابط الوصول للجهاز الرئيسي.txt"

echo.
echo      اتسجلت العناوين في: رابط الوصول للجهاز الرئيسي.txt
echo      شغّل من جهاز خارجي ملف: الاتصال من خارج البيت.bat
echo      الرابط العام مش بيفتح غير بعد فتح المنفذ في الراوتر.
echo      راجع: دليل الربط من خارج البيت.txt
echo.
pause