# 🚀 פריסת שרת הרישוי ל־Railway

מדריך מלא מאפס ועד ש־URL ציבורי חי בידיים שלך.
כל הקבצים שצריך ל־Railway כבר מוכנים בתיקיית `server/`.

## מה הוכן לך

| קובץ | תפקיד |
|------|-------|
| `Procfile` | אומר ל־Railway איך להריץ את השרת (`uvicorn` על `$PORT`) |
| `railway.toml` | הגדרות build + deploy + health check |
| `runtime.txt` | מנעל את גרסת Python ל־3.11.9 |
| `requirements.txt` | רשימת חבילות (קיים מראש) |
| `.railwayignore` | קבצים שלא ישלחו ל־Railway (`.env`, DB, pyc) |
| `.gitignore` | קבצים שלא ייכנסו ל־git |

## שלבים — הפעם הראשונה

### 1. צור חשבון Railway

1. לך ל־https://railway.app/
2. Sign up עם GitHub (הכי פשוט)
3. אשר את החשבון

### 2. התקן Railway CLI (לפריסה מהמחשב ישירות)

```powershell
# ב־PowerShell של Windows
npm install -g @railway/cli
```

אם אין לך Node.js, הורד מ־https://nodejs.org/ (Recommended LTS).

### 3. היכנס ל־Railway מה־CLI

```powershell
railway login
```

יפתח לך דפדפן — אשר את ההתחברות.

### 4. פרוס את השרת

פתח terminal ב־**תיקיית `server/`** (חשוב — לא התיקייה האב):

```powershell
cd "C:\Users\elira.LAPTOP-RAPHSBRS\Desktop\magnet fraime PRO\server"
railway init
```

יציג:
- `Would you like to create a new project? (Y/n)` — לחץ `Y`
- שם הפרויקט — למשל `magnet-frame-license-server`
- יבחר לך environment (production בברירת מחדל) — אישור

ואז:

```powershell
railway up
```

השלב הזה:
- אורז את תיקיית `server/` (בלי `.env` ו־DB — `.railwayignore` מסנן)
- שולח ל־Railway
- Railway בונה Docker image עם Python 3.11
- מתקין את התלויות מ־`requirements.txt`
- מפעיל את השרת לפי ה־`Procfile`

התהליך לוקח 2-4 דקות בפעם הראשונה.

### 5. הגדר משתני סביבה (חשוב!)

בלי זה השרת **ימות בעלייה**. מפעיל CLI ישירות:

```powershell
railway variables set ADMIN_USERNAME=admin
railway variables set ADMIN_PASSWORD=YOUR_SECURE_PASSWORD_HERE
railway variables set SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

או דרך ה־dashboard של Railway:
1. היכנס ל־https://railway.app/
2. בחר את הפרויקט שלך
3. Variables → Add Variable

**משתנים חובה:**

| שם | ערך מומלץ | הערה |
|----|----------|------|
| `ADMIN_USERNAME` | `admin` (או משהו אחר) | משתמש להתחברות לדשבורד הניהול |
| `ADMIN_PASSWORD` | סיסמה חזקה (16+ תווים) | הסיסמה של admin |
| `SECRET_KEY` | 64 תווים רנדומלי | חותם cookies — ייצר אחד פעם אחת ואל תשנה |
| `DATABASE_PATH` | `/data/licenses.db` | נתיב קובץ ה־SQLite בתוך Volume (ראה שלב 6) |

**משתנים אופציונליים (רק אם רוצה מיילים):**

| שם | ערך | הערה |
|----|-----|------|
| `SMTP_HOST` | `smtp.gmail.com` | שרת SMTP למיילים |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | `your@gmail.com` | |
| `SMTP_PASSWORD` | `app-password` | Gmail App Password — לא הסיסמה הרגילה |
| `SMTP_FROM` | `noreply@magnetframe.com` | |
| `SMTP_FROM_NAME` | `Magnet Frame Pro` | |
| `SMTP_USE_TLS` | `true` | |
| `WEBHOOK_SECRET` | random string | רק אם משתמש בתשלומים |

### 6. הוסף Volume לשמירת ה־DB (קריטי!)

⚠️ **קובץ SQLite יימחק בכל deploy בלי Volume.** Railway עובד על filesystem ארעי.

1. ב־dashboard של Railway → פרויקט שלך → ה־service
2. **Settings → Volumes → Add Volume**
3. Mount Path: `/data`
4. Size: 1 GB (הרבה יותר מדי, אבל בחינם)
5. Save

ואז עדכן את משתנה הסביבה `DATABASE_PATH`:
```
DATABASE_PATH=/data/licenses.db
```

Railway יעשה re-deploy אוטומטי. מעכשיו ה־SQLite שלך על Volume — שורד restarts, deploys, הכל.

### 7. קבל את ה־URL הציבורי

ב־dashboard של Railway → ה־service שלך → **Settings → Networking → Generate Domain**.

יצור לך URL כמו:
```
https://magnet-frame-license-server-production.up.railway.app
```

### 8. בדוק שהכל עולה

פתח את ה־URL בדפדפן + הוסף סיומת:
```
https://xxx.up.railway.app/license/whoami
```

אמור להחזיר JSON כמו:
```json
{"ip": "your-ip-here"}
```

ואז דשבורד הניהול:
```
https://xxx.up.railway.app/admin
```

התחבר עם `ADMIN_USERNAME` / `ADMIN_PASSWORD` שהגדרת.

### 9. חבר את התוכנה ל־URL הזה

ב־`config.py` בפרויקט הלקוח (לא בשרת):
```python
LICENSE_SERVER_URL = "https://xxx.up.railway.app"
```

Rebuild את התוכנה ותשלח ללקוחות. זהו — השרת חי, התוכנה מדברת איתו.

---

## פעולות יום־יום אחרי הפריסה הראשונה

### עדכון הקוד של השרת

```powershell
cd "C:\Users\elira.LAPTOP-RAPHSBRS\Desktop\magnet fraime PRO\server"
# עשה שינויים בקוד...
railway up
```

זמן: 1-2 דקות. אפס downtime (Railway מחליף instances בצורה rolling).

### לראות logs חיים

```powershell
railway logs
```

או ב־dashboard: Deployments → latest → Logs.

### גיבוי ה־DB

```powershell
# Download the DB from the server
railway run --service=your-service bash -c "cat /data/licenses.db" > local_backup.db
```

### עצירה / הפעלה מחדש

ב־dashboard → Settings → Pause/Resume.

---

## מעבר שרת בעתיד — בלי לעדכן את התוכנה

### האופציה הנקייה: דומיין משלך

1. קנה דומיין (Cloudflare / NameCheap — 30-60 ₪ לשנה)
2. ב־Railway → Settings → Networking → **Custom Domain**
3. הוסף `license.yourdomain.com`
4. Railway ייתן לך CNAME לכוון אליו — הוסף ב־DNS של הדומיין שלך
5. Railway יוצר SSL אוטומטית (דקות)
6. **שנה ב־`config.py` של התוכנה**: `LICENSE_SERVER_URL = "https://license.yourdomain.com"`
7. Build אחד, שולח ללקוחות

מעכשיו — אם יום אחד תעבור מ־Railway ל־AWS:
- אתה משאיר את הדומיין ורק מעדכן את ה־DNS־record שמצביע למקום החדש
- **התוכנה של הלקוחות ממשיכה לעבוד** — אפס עדכון

### האופציה הזמנית: URL של Railway

עובד, אבל אם Railway ישנה את ה־URL הכללי שלו בעתיד (לא סביר אבל אפשרי) — תצטרך עדכון לקוחות. לא מומלץ לטווח ארוך.

---

## פתרון בעיות נפוצות

### השרת לא עולה — `RuntimeError: ADMIN_USERNAME not set`
הגדר את `ADMIN_USERNAME` ו־`ADMIN_PASSWORD` כמשתני סביבה ב־Railway. ראה שלב 5.

### ה־DB נעלם אחרי כל deploy
לא הוגדר Volume. ראה שלב 6.

### שגיאת CORS מהתוכנה
`main.py` של השרת כבר מגדיר CORS פתוח (`allow_origins=["*"]`). אם יש בעיה — בדוק שהתוכנה פונה ל־HTTPS (לא HTTP) של השרת.

### ה־deploy נכשל עם `ModuleNotFoundError`
חבילה חסרה ב־`requirements.txt`. הוסף אותה ו־`railway up` מחדש.

### `healthcheck failed`
השרת לא עולה תוך 30 שניות. הגדל את הזמן ב־`railway.toml`:
```toml
healthcheckTimeout = 60
```

---

## אבטחה — רשימת תיוג לפני live

- [ ] `ADMIN_PASSWORD` סיסמה **חזקה** (לפחות 16 תווים, מעורב)
- [ ] `SECRET_KEY` **נוצר רנדומלי** (64 hex), לא סיסמה קלה
- [ ] הדשבורד `/admin` מוגן בסיסמה (אוטומטית לאחר שהגדרת משתמש)
- [ ] מעבר ל־HTTPS בלבד (Railway אוטומטי)
- [ ] Volume מוגדר ל־DB (אחרת אובדן נתונים)
- [ ] גיבוי תקופתי של ה־DB (בזמן שאין עוד מנגנון מסודר)

---

## עלות

Railway נותן **$5 קרדיט חינם בחודש**. שרת רישוי קטן (עד ~10K בקשות ביום) נכנס בזה בקלות.

אם תעבור את התקרה — $5/חודש על כל unit נוסף. זול מאוד.

---

## שאלות?

אם משהו לא עולה כמו שצריך, תייצר `railway logs` ונחקור יחד את השגיאה.
