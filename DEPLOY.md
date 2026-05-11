# 🚀 העלאה לאוויר — Magnet Frame PRO

מדריך **שלב-אחר-שלב** להעלות את האתר ל-`magnetframepro.co.il` דרך Railway.

הקוד **כבר מוכן** ל-production. צריך רק 4 שלבים: GitHub → Railway → Volume → DNS.

---

## 📋 Checklist מהיר

- [ ] שלב 1: יצירת ריפו GitHub והעלאת הקוד
- [ ] שלב 2: יצירת פרויקט Railway מהריפו
- [ ] שלב 3: הוספת Volume + הגדרת Environment Variables
- [ ] שלב 4: חיבור הדומיין `magnetframepro.co.il` ב-JetServer DNS
- [ ] שלב 5: בדיקה סופית

---

## שלב 1️⃣ — GitHub

### 1.1 ליצור ריפו חדש
1. כנס ל-https://github.com/new
2. שם: `magnet-frame-pro` (פרטי, **Private**)
3. אל תוסיף README / .gitignore / license (יש לנו כבר)
4. לחץ "Create repository"
5. **העתק את ה-URL** (`https://github.com/USERNAME/magnet-frame-pro.git`)

### 1.2 העלאת הקוד
פתח **PowerShell** בתיקיית הפרויקט:
```powershell
cd "C:\Users\elira_z18n7lv\OneDrive\Desktop\magnet fraime PRO"
git init
git add .
git commit -m "Initial commit — production-ready"
git branch -M main
git remote add origin https://github.com/USERNAME/magnet-frame-pro.git
git push -u origin main
```
(החלף `USERNAME` בשם המשתמש שלך ב-GitHub)

⚠️ אם git לא מותקן: הורד מ-https://git-scm.com/download/win

---

## שלב 2️⃣ — Railway

### 2.1 יצירת פרויקט
1. כנס ל-https://railway.app/new
2. לחץ "**Deploy from GitHub repo**"
3. אם זו פעם ראשונה: אשר את Railway לגשת ל-GitHub שלך
4. בחר את הריפו `magnet-frame-pro`
5. Railway מתחיל לזהות פרויקט Python אוטומטית (יש לנו `requirements.txt` + `Procfile`)

### 2.2 הגדרת תיקיית השרת
Railway צריך לדעת שהאפליקציה ב-`server/`:
1. בכרטיס השירות → **Settings**
2. גלילה ל-**Build** → "Root Directory" → הקלד: `server`
3. שמור

זה אומר ש-Railway יריץ `pip install -r requirements.txt` ו-`python -m uvicorn app.combined_main:app` בתוך `server/`.

---

## שלב 3️⃣ — Volume + Environment Variables

### 3.1 הוספת Volume (חיוני! בלעדיו ה-DB נמחק כל deploy)
1. בשירות → **Settings** → גלילה ל-**Volumes**
2. לחץ "**+ New Volume**"
3. Mount Path: `/data`
4. Size: 1GB (מספיק לאלפי הזמנות)
5. שמור

### 3.2 הגדרת Variables
בשירות → **Variables** → לחץ "**+ New Variable**" ל-**כל אחד** מאלה:

| משתנה | ערך | הערה |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | שם המשתמש שלך לכניסה לאדמין |
| `ADMIN_PASSWORD` | **סיסמה חזקה**, 16+ תווים | ⚠️ אל תשתף עם איש |
| `SECRET_KEY` | מחרוזת רנדומלית 32+ תווים | פקודה למטה |
| `MFP_ALLOWED_ORIGINS` | `https://magnetframepro.co.il,https://www.magnetframepro.co.il` | |
| `DATABASE_PATH` | `/data/licenses.db` | חייב להיות בתוך ה-Volume! |
| `SESSION_LIFETIME_DAYS` | `30` | |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | `PHOTO.ME.PM1@GMAIL.COM` | |
| `SMTP_PASSWORD` | App Password מ-Google | ראה הוראות למטה |
| `SMTP_FROM` | `PHOTO.ME.PM1@GMAIL.COM` | |
| `SMTP_FROM_NAME` | `Magnet Frame PRO` | |
| `SMTP_USE_TLS` | `true` | |

### 🔐 איך מייצרים סיסמאות וערכים?

**SECRET_KEY** — הפעל ב-PowerShell:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
העתק את הפלט ושים ב-`SECRET_KEY`.

**ADMIN_PASSWORD** — תוכל לכתוב משהו ידני (16+ תווים), או:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(16))"
```

**SMTP_PASSWORD (Gmail App Password)**:
1. כנס ל-https://myaccount.google.com/security
2. ודא ש"2-Step Verification" מופעל (חובה)
3. חפש "App passwords" (סיסמאות אפליקציה)
4. צור חדש עם שם "Magnet Frame PRO"
5. גוגל ייתן לך **16 תווים** — העתק אותם בדיוק כפי שהם

### 3.3 Deploy
אחרי שכל ה-variables מוגדרות, לחץ "**Deploy**" (או רענן את הדף — Railway עושה deploy אוטומטי כשמשנים variables).

המתן ~2 דקות עד שמופיע "**Success**" עם URL כמו `magnet-frame-pro-production.up.railway.app`.

### 3.4 בדיקת חיים ראשונה
פתח בדפדפן:
- `https://YOUR-RAILWAY-URL.up.railway.app/health` → אמור להחזיר `{"ok": true}`
- `https://YOUR-RAILWAY-URL.up.railway.app/site/` → האתר מופיע בדומיין הזמני
- `https://YOUR-RAILWAY-URL.up.railway.app/admin/login` → עמוד התחברות לאדמין

🎉 אם הכל עובד — הקוד באוויר! עכשיו רק להחליף את ה-URL ב-`magnetframepro.co.il`.

---

## שלב 4️⃣ — DNS ב-JetServer

### 4.1 קבלת ה-IP של Railway
בכרטיס השירות ב-Railway:
1. **Settings** → גלילה ל-**Networking**
2. **Custom Domain** → לחץ "**+ Custom Domain**"
3. הקלד: `magnetframepro.co.il`
4. Railway יציג לך **2 רשומות DNS** להוסיף — לפי הסוג:
   - `A` record (IP כמו `66.33.22.11`)
   - או `CNAME` record (כמו `xxx.up.railway.app`)
5. **השאר את הדף פתוח** — נחזור אליו

### 4.2 הוספה ב-JetServer
1. כנס ל-https://jetclients.co.il/clientarea.php?action=domains
2. ליד `magnetframepro.co.il` → **ניהול DNS**
3. הוסף **2 רשומות**:

| Type | Name | Value | TTL |
|---|---|---|---|
| `A` או `CNAME` | `@` | (מה ש-Railway נתן) | `3600` |
| `CNAME` | `www` | (מה ש-Railway נתן ל-www) | `3600` |

4. שמור

### 4.3 חזרה ל-Railway והוספת `www`
חזור ל-Railway → Custom Domain:
- הוסף עוד custom domain: `www.magnetframepro.co.il` (זה יפנה לאותו שרת)

### 4.4 המתנה ל-DNS Propagation
תוך **5 דקות עד שעה**, הדומיין יתחיל לעבוד. בדוק:
```powershell
nslookup magnetframepro.co.il 8.8.8.8
```
אם זה מחזיר IP — אנחנו בעניינים.

Railway מנפיק **תעודת SSL חינמית** (Let's Encrypt) אוטומטית תוך עוד 10 דקות.

---

## שלב 5️⃣ — בדיקה סופית 🎉

1. פתח `https://magnetframepro.co.il/site/` — האתר אמור להופיע
2. פתח `https://magnetframepro.co.il/admin/login` — אמור להופיע מסך התחברות
3. התחבר עם `admin` + ה-`ADMIN_PASSWORD` שהגדרת
4. בצע רכישת בדיקה דרך האתר → אמור להופיע ב-`/admin/customers`
5. בדוק שקיבלת אימייל עם המפתח

### 🔒 אבטחה אחרי שזה באוויר
- ❌ אל תשתף את ה-`ADMIN_PASSWORD` בשום מקום
- ❌ אל תעלה את `.env` ל-GitHub
- ✅ הסיסמה ניתן לאיפוס דרך SSH ל-Railway אם תשכח
- ✅ אם חושד שמישהו ניסה לפרוץ — שנה את `ADMIN_PASSWORD` ב-Variables ועשה redeploy

---

## 🆘 בעיות נפוצות

### "Application failed to start"
→ פתח את ה-Logs ב-Railway. בדרך כלל זה env var חסר. ודא ש-`DATABASE_PATH` מצביע ל-`/data/...` והVolume מותקן.

### "Database is locked"
→ Volume לא מותקן. הוסף Volume → Mount Path `/data` ועשה redeploy.

### האימייל לא נשלח
→ ודא ש-SMTP_PASSWORD הוא App Password (16 תווים מ-Google), לא הסיסמה הרגילה.

### הדומיין לא מגיע ל-Railway
→ DNS לוקח עד 24 שעות לפעמים. נסה לבדוק ב-`nslookup` (פקודה למעלה). אם DNS תקין אבל Railway נותן 404 — ודא ש-Custom Domain מוגדר נכון.

---

## 💰 עלויות חודשיות

| פריט | מחיר |
|---|---|
| Railway (חבילה $5 חודשית) | $5 (~18₪) |
| Volume 1GB | חינם (בתוך התוכנית) |
| SSL / HTTPS | חינם |
| Bandwidth (עד ~100GB/חודש) | חינם |
| דומיין `.co.il` | ~50₪/שנה (4₪/חודש) |
| **סה״כ חודשי** | **~22₪** |

---

🎯 **מוכן? עקוב צעד-אחר-צעד. אם תיתקע באיזה שלב — תאמר לי בדיוק איפה ואני אעזור.**
