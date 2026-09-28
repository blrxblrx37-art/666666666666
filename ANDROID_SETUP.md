# تشغيل •-تــيــ۾ إڪـXـس-• على Android عبر Termux

## ملاحظات مهمة

- التشغيل على الهاتف مناسب للتجربة أو الاستخدام الشخصي، وليس أفضل خيار لاستضافة عامة 24/7.
- Android قد يوقف العمليات في الخلفية؛ استخدم Termux:Boot وأوقف Battery Optimization لـTermux.
- الموقع سيكون متاحًا على الهاتف عبر `http://127.0.0.1:5000`.
- من جهاز آخر على نفس شبكة Wi‑Fi استخدم عنوان الهاتف المحلي مثل `http://192.168.1.20:5000`.
- نشر الموقع للعامة من الهاتف يحتاج Port Forwarding أو نفقًا آمنًا، ولا يُنصح بفتح لوحة الإدارة مباشرة للإنترنت.

## 1. تثبيت Termux

ثبّت Termux من [F-Droid](https://f-droid.org/packages/com.termux/) ثم افتحه.

نفّذ:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python clang nodejs php unzip git termux-api
pip install --upgrade pip
```

لذلك يدعم المشروع Python وNode.js وPHP وC/C++ حسب الحزم المتاحة على الجهاز.

## 2. نقل المشروع

نزّل ملف `team-x-site-final.zip` إلى الهاتف، ثم داخل Termux:

```bash
termux-setup-storage
mkdir -p ~/team-x-site
unzip /sdcard/Download/team-x-site-final.zip -d ~/team-x-site
cd ~/team-x-site
pip install -r requirements.txt
```

## 3. تشغيل الموقع

```bash
cd ~/team-x-site
export SECRET_KEY="غيّر-هذا-إلى-قيمة-طويلة-عشوائية"
export ADMIN_USERNAME="Admin@gmail.com"
export ADMIN_PASSWORD="Admin12"
export PORT=5000
termux-wake-lock
python app.py
```

افتح متصفح الهاتف على:

```text
http://127.0.0.1:5000/login
```

بيانات الإدارة:

```text
Username: Admin@gmail.com
Password: Admin12
```

## 4. التشغيل التلقائي بعد إعادة تشغيل الهاتف

ثبّت تطبيق **Termux:Boot** من F-Droid، ثم نفّذ داخل Termux:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-ygh.sh <<'SH'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd "$HOME/team-x-site"
export SECRET_KEY="غيّر-هذا-إلى-قيمة-طويلة-عشوائية"
export ADMIN_USERNAME="Admin@gmail.com"
export ADMIN_PASSWORD="Admin12"
export PORT=5000
exec python app.py >> "$HOME/ygh.log" 2>&1
SH
chmod +x ~/.termux/boot/start-ygh.sh
```

بعد ذلك:

1. افتح إعدادات Android.
2. عطّل Battery Optimization لكل من Termux وTermux:Boot.
3. اسمح للتطبيقين بالعمل في الخلفية.
4. أعد تشغيل الهاتف.
5. راجع السجل عند الحاجة:

```bash
tail -f ~/ygh.log
```

## 5. الوصول من جهاز آخر على نفس الشبكة

اعرف عنوان الهاتف:

```bash
ip addr show wlan0
```

ابحث عن قيمة `inet` مثل `192.168.1.20`، ثم افتح من الجهاز الآخر:

```text
http://192.168.1.20:5000/login
```

لا تستخدم Port Forwarding للوحة الإدارة إلا مع HTTPS وحماية إضافية؛ الهاتف قد يتغير عنوانه أو يتوقف بسبب النظام.
