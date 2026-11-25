# Isaiev AI Autopost

Безкоштовна програма, яка за розкладом публікує пости в **Instagram, Threads, LinkedIn** (і за бажанням у **Telegram**). Працює на GitHub Actions у хмарі, тож комп'ютер може бути вимкнений. Публікує лише через офіційні API мереж.

## Як це працює

1. Кожен пост лежить у своїй папці в `posts/`: це файл `post.yaml` і картинки.
2. Кожні 15 хвилин GitHub запускає програму. Вона публікує ті пости, чий час уже настав.
3. Результат (посилання на пост або помилка) програма записує в `published.json` у папці поста. Опублікований пост ніколи не публікується вдруге.
4. Якщо мережа відповіла помилкою, програма пробує ще 2 рази. Якщо помилка остаточна, GitHub надсилає тобі лист.

## Крок 1. GitHub (≈10 хв)

1. Зареєструйся на github.com, якщо ще немає акаунта.
2. Створи новий репозиторій `isaiev-autopost` і завантаж у нього всі файли з архіву.
   - **Репозиторій має бути Public.** Instagram і Threads забирають картинки лише за публічним посиланням.
   - Секрети (токени) при цьому ніхто не бачить, вони зберігаються окремо.
   - Мінус такий: картинки посту видно в репозиторії ще до публікації.
3. Відкрий Settings → Actions → General → Workflow permissions і вибери **Read and write permissions**.

Усі ключі нижче додаються в **Settings → Secrets and variables → Actions → New repository secret**.

## Крок 2. Instagram + Threads (≈30 хв, один раз)

Потрібен професійний акаунт Instagram (Business або Creator).

1. Зайди на developers.facebook.com → My Apps → **Create app**.
2. Вибери варіант використання **Instagram API (Instagram Login)**. У розділі API setup додай свій акаунт `isaiev_ai` як тестувальника, підтвердь запрошення в Instagram і натисни **Generate token**.
   - Дозволи: `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`.
   - Секрети: `IG_ACCESS_TOKEN` (токен) і `IG_USER_ID` (ID акаунта, який показує та сама сторінка).
3. У тому ж застосунку додай варіант використання **Threads API** → додай тестувальника Threads → **User Token Generator**.
   - Дозволи: `threads_basic`, `threads_content_publish`.
   - Секрет: `THREADS_ACCESS_TOKEN`.
4. Застосунок можна залишити в режимі розробки, бо публікуєш лише у власні акаунти.
5. Токени діють 60 днів. Щоб вони оновлювались самі, створи на github.com → Settings → Developer settings → Fine-grained token доступ до цього репозиторію з правом **Secrets: Read and write**. Збережи його як секрет `GH_PAT`. Після цього щотижневий workflow `refresh-tokens` подовжуватиме токени.

## Крок 2b. Facebook-сторінка + Instagram через сторінку (рекомендовано)
Цей шлях не потребує бізнес-портфоліо Meta.
1. Instagram (професійний акаунт) підключений до сторінки Facebook (Meta Business Suite → «Connect Instagram»).
2. У додатку Meta (developers.facebook.com) є дозволи `pages_manage_posts`, `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish`.
3. Graph API Explorer → вибрати додаток → User Token з цими дозволами → обміняти на довгий токен → `GET /me/accounts` дає **Page token** (безстроковий).
4. `GET /{page-id}?fields=instagram_business_account` дає id Instagram.
5. Секрети GitHub: `FB_PAGE_ID`, `FB_PAGE_ACCESS_TOKEN`, `IG_USER_ID` (id з п.4). `IG_ACCESS_TOKEN` можна не задавати: Instagram візьме токен сторінки.

Потім у `post.yaml` замість `instagram: {enabled: false}` дати блок `instagram:` (caption), і `facebook:` (text, images або `video`). У застосунку Instagram вимкнути «Поширення в Threads», щоб Threads постив лише autopost. Перед постом autopost також читає власні пости Instagram і сторінки Facebook за 48 год і не дублює їх. Для Instagram `video: clip.mp4` публікується як Reels (`thumb_offset` у мс або `cover: cover.jpg`).

## Крок 3. LinkedIn (≈20 хв)

**Чому не другий профіль:** LinkedIn дозволяє одній людині мати лише один профіль. Тому для бренду створи **сторінку компанії «Isaiev AI»**: «Для бізнесу» → «Створити сторінку компанії». Вона знадобиться і для застосунку.

1. Зайди на linkedin.com/developers → **Create app** і прив'яжи його до сторінки Isaiev AI.
2. У вкладці Products додай **Sign In with LinkedIn using OpenID Connect** і **Share on LinkedIn**.
3. У вкладці Auth додай Redirect URL `http://localhost:8765/callback`.
4. На своєму комп'ютері виконай:
   ```
   pip install requests
   python tools/linkedin_auth.py <Client ID> <Client Secret>
   ```
   Скрипт видасть значення для секретів `LINKEDIN_ACCESS_TOKEN` і `LINKEDIN_AUTHOR_URN`.
5. Цей токен діє 60 днів і сам не оновлюється. Раз на 2 місяці запускай скрипт ще раз.

**Публікація на сторінку компанії:** для цього LinkedIn вимагає окремий доступ (Community Management API). Його дають лише після заявки й перевірки. Поки доступу немає, програма постить у твій особистий профіль. Коли доступ дадуть, заміни `LINKEDIN_AUTHOR_URN` на `urn:li:organization:<ID сторінки>`.

Якщо LinkedIn відповідає, що версія API неактивна: Settings → Variables → створи змінну `LINKEDIN_VERSION` зі значенням поточного місяця, наприклад `202610`.

## Крок 4 (за бажанням). Telegram-канал

Створи бота через @BotFather і додай його адміністратором каналу. Потім заповни секрети:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`: це `@назва_каналу`

## Як додати пост

Створи папку `posts/2026-10-29_99pct_ua/` і поклади туди картинки та `post.yaml` (зразок лежить у `posts/_example/`):

```yaml
publish_at: "2026-10-29 10:00"      # час Europe/Rome
images: [01.png, 02.png, 03.png]    # порядок слайдів; PNG сам перетвориться на JPG для Instagram
instagram:
  caption: |
    Текст підпису...
threads:
  publish_at: "2026-10-29 10:15"
  text: "Короткий текст до 500 символів"
linkedin:
  text: |
    Текст для LinkedIn...
```

- Щоб вимкнути мережу для поста, видали її блок або напиши `enabled: false`.
- `draft: true` означає, що пост лежить, але не публікується.
- Папки, назва яких починається з `_`, програма пропускає.

## Перевірка без публікації

Відкрий Actions → autopost → **Run workflow** і постав галочку `dry_run`. Програма покаже таблицю всіх постів і що саме опублікувала б.

## Ліміти мереж (програма перевіряє їх сама)

| Мережа | Текст | Картинки |
|---|---|---|
| Instagram | до 2200 символів, до 30 хештегів | 1–10, лише JPG (конвертація автоматична) |
| Threads | до 500 символів | 0–20 |
| LinkedIn | до 3000 символів | 0–20 |
| Telegram | 1024 з картинками / 4096 без | 0–10 |

TikTok і YouTube поки не підключені. Через власний застосунок вони публікують лише приватні відео, поки застосунок не пройде їхню перевірку.

## Захист від дублів (duplicate gate)

Перед **кожною** публікацією `autopost run` перевіряє, чи цей пост уже виходив у цій мережі:

- **Реєстр** `posts/_ledger.json`: усе, що опублікував autopost (slug поста + мережа + «хук» — перші 80 символів підпису, малими літерами, без емодзі й #тегів). Записи також відновлюються з `published.json` кожного поста. Пост, зроблений поза autopost, можна дописати в реєстр руками.
- **Сам акаунт Threads** (@isaiev_ai і @isaiev_vasyl): його пости за останні 48 год через Threads API.

Якщо збігся slug або перші 40 символів хука, мережа пропускається: у лозі `DUPLICATE BLOCKED`, у таблиці підсумку рядок 🛑, у `published.json` статус `duplicate_blocked` (більше не повторюється).

Свідомий репост: `allow_repost: true` у `post.yaml` (на верхньому рівні для всіх мереж або в блоці однієї мережі), потім прибрати `duplicate_blocked` із `published.json`, якщо пост уже заблоковано.

`python -m autopost ledger` перебудовує реєстр із `published.json` (ручні записи лишаються).

Прибрати дубль, що вже вийшов (Threads): додати запис у `posts/_delete.json` (`network`, `id`, `permalink`, `slug`, `reason`). Наступний запуск autopost перевірить, що id веде саме на цей permalink, видалить пост через Threads API і позначить його `deleted_duplicate` (повторно він не публікується).

**Кейс і кросспости з Instagram.** Instagram сам дублює пости в Threads («Share to Threads») зі своїм підписом, тому однакового тексту мало. Кожен пост має `case`: ключ `case:` у `post.yaml`, інакше береться з назви папки без дати (`2026-11-23_salt_mirror` → `salt-mirror`; серії з цифрами, як `italy05`, кейсу не мають). Перед постом у Threads блокує також той самий кейс у реєстрі або пост на акаунті за 48 год, що називає кейс чи має 3+ спільних змістовних слова в перших 80 символах. Кожен запуск autopost також переглядає @isaiev_ai і @isaiev_vasyl за 48 год і новішу копію дубля ставить у `posts/_delete.json` (видаляється, коли токен має дозвіл `threads_delete`).

**Правило власника (25.11): видаляється лише 100% дублікат**: той самий повний текст і той самий тип медіа. Якщо пост хоч трохи відрізняється (інший підпис, інший формат, карусель чи відео), він НЕ дублікат і не видаляється. Записи зі статусом `keep` у `posts/_delete.json` не видаляються ніколи.

**Докази перед видаленням (власник 25.11).** Прямо перед кожним видаленням autopost заново читає обидва пости через API і видаляє копію лише якщо: оригінал записаний і досі живий; повний текст однаковий; тип медіа однаковий; копія новіша; у копії не більше переглядів/вподобайок, ніж в оригіналу. Інакше запис отримує статус `keep` з поясненням у `evidence` і більше ніколи не видаляється. Усі докази зберігаються в `posts/_delete.json`.

**Лише з дозволу власника.** Навіть доведений дублікат не видаляється сам: запис отримує статус `pending_owner`. Видалення відбувається тільки після того, як власник побачив докази і написав «так» саме для цього посилання; тоді в записі ставиться `"owner_approved": true`.
