 [English](securing-with-npm-and-cloudflare.md) | Русский

# Безопасный публичный деплой через Nginx Proxy Manager и Cloudflare Access

Эта инструкция превращает локальный стек `agent-orchestrator-lite` (привязанный
к `127.0.0.1`) в публичный HTTPS-деплой, защищённый **двумя независимыми
слоями**:

1. **Nginx Proxy Manager (NPM)** — терминирует TLS, проксирует трафик в
   контейнеры, включает HTTP Basic Auth через *Access List* и удерживает
   WebSocket Streamlit.
2. **Cloudflare Zero Trust / Access** — ставит перед UI идентификационный гейт
   (Email PIN / OTP), при этом пути API и webhook остаются доступными для
   автоматизации.

Порядок важен: запрос идёт `клиент → edge Cloudflare → NPM → контейнер`.
Поэтому NPM не должен быть доступен из интернета напрямую — ограничьте его
диапазонами IP Cloudflare либо приватным/Tailscale-интерфейсом.

---

## Что получится

```
   браузер / CI ──► ┌──────────────────────────────┐
                    │ Cloudflare edge (TLS, WAF)   │
                    │  Access app  ui.example.com  │  ← Email PIN / OTP
                    │  Access app  api.example.com │  ← или Service Token
                    └──────────────┬───────────────┘
                                   │ HTTPS (Full / strict)
                    ┌──────────────▼───────────────┐
                    │ Nginx Proxy Manager :443     │
                    │  Access List → Basic Auth    │
                    │  Websockets Support → ON     │
                    └───────┬──────────────┬───────┘
                            │              │
                 ┌──────────▼───┐   ┌──────▼─────────────┐
                 │ agent-ui     │   │ agent-orchestrator │
                 │ Streamlit    │   │ FastAPI Gateway    │
                 │ :8501        │   │ :8000 (host :8085) │
                 └──────────────┘   └────────────────────┘
```

Два хоста выбраны осознанно:

| Хост | Upstream (контейнер:порт) | Назначение |
|---|---|---|
| `ui.example.com` | `agent-ui:8501` | Streamlit Dashboard: Access + Basic Auth |
| `api.example.com` | `agent-orchestrator:8000` | Gateway API (`/health`, `/api/v1/plan/direct`): service token и/или Basic Auth |

> Предпочитайте **отдельный поддомен на сервис**, а не маршрутизацию по путям.
> Streamlit считает, что владеет корнем сайта: работа из подпути требует
> `--server.baseUrlPath` и ломает статику и WebSocket-эндпоинт.

---

## Требования

- Домен, DNS которого управляется Cloudflare (NS-записи указывают на
  Cloudflare).
- Linux-хост с Docker и Docker Compose, стек из этого репозитория запущен
  (`docker compose up -d`).
- Nginx Proxy Manager (отдельный контейнер или установка на хосте), админка на
  порту `81` доступна с вашей рабочей машины.
- Порты `80` и `443` открыты в фаерволе хоста только для Cloudflare.

Никогда не публикуйте порты оркестратора напрямую. `temporal` (`7233`),
`temporal-ui` (`8233`) и `openhands` (`8000`) должны оставаться приватными — у
них нет собственной аутентификации.

## Шаг 1 — Дайте NPM доступ к контейнерам

По умолчанию все сервисы в `docker-compose.yml` слушают
`${BIND_IP:-127.0.0.1}`, то есть loopback **Docker-хоста**. У контейнера NPM в
bridge-сети свой loopback, поэтому `127.0.0.1:8501` внутри NPM указывает на сам
NPM. Выберите один из двух вариантов.

### Вариант A (рекомендуется) — общая Docker-сеть

Compose-проект называется `agent-orchestrator-lite`, значит его сеть —
`agent-orchestrator-lite_agent-net`:

```bash
docker network ls | grep agent-net
# agent-orchestrator-lite_agent-net   bridge   local
```

Если NPM запускается из собственного `docker-compose.yml`, объявите эту сеть
как внешнюю:

```yaml
services:
  app:
    image: 'jc21/nginx-proxy-manager:latest'
    restart: unless-stopped
    ports:
      - '80:80'
      - '443:443'
      - '81:81'
    volumes:
      - ./data:/data
      - ./letsencrypt:/etc/letsencrypt
    networks:
      - npm-net

networks:
  npm-net:
    name: agent-orchestrator-lite_agent-net
    external: true
```

Если NPM уже работает, подключите его без пересоздания контейнера:

```bash
docker network connect agent-orchestrator-lite_agent-net nginx-proxy-manager
```

Теперь upstream — это просто имена сервисов: `agent-ui` → `8501` и
`agent-orchestrator` → `8000`. Порты хоста публиковать не нужно вообще, поэтому
`BIND_IP=127.0.0.1` можно оставить как есть.

### Вариант B — публикация на приватном интерфейсе

Укажите в `.env` в `BIND_IP` LAN- или Tailscale-адрес хоста, перезапустите стек
и направьте NPM на `<ip-хоста>:8501` / `<ip-хоста>:8085`:

```bash
# .env
BIND_IP=100.64.0.10      # пример адреса Tailscale — никогда не 0.0.0.0 на публичном хосте
```

```bash
docker compose up -d
```

При `BIND_IP=0.0.0.0` контейнеры слушают все интерфейсы, поэтому фаервол хоста
обязан отбрасывать всё, кроме `80`/`443` от диапазонов Cloudflare.

---

## Шаг 2 — Создайте proxy hosts в NPM

`Hosts → Proxy Hosts → Add Proxy Host` — по одному на каждый хост.

### `ui.example.com` — Streamlit

**Вкладка Details**

| Поле | Значение |
|---|---|
| Domain Names | `ui.example.com` |
| Scheme | `http` |
| Forward Hostname / IP | `agent-ui` (вариант A) либо IP хоста (вариант B) |
| Forward Port | `8501` |
| Block Common Exploits | ON |
| **Websockets Support** | **ON** — см. шаг 5, без этого UI «зависает» |
| Access List | список из шага 3 |

**Custom locations** — для отдельного поддомена не нужны, оставьте пустыми.

**Вкладка SSL** — выберите сертификат из шага 4, затем:

| Поле | Значение |
|---|---|
| Force SSL | ON |
| HTTP/2 Support | ON |
| HSTS Enabled | ON (*HSTS Subdomains* — только если весь домен работает по HTTPS) |

### `api.example.com` — FastAPI Gateway

Та же форма, `Forward Port` = `8000`, **Websockets Support OFF** (API — обычный
HTTP). `Block Common Exploits` оставьте ON.

> Порт `8085` — это только *внешний* маппинг контейнерного `8000`. Внутри
> Docker-сети всегда используйте `agent-orchestrator:8000`; `8085` там не
> разрешается.

### `temporal.example.com` — создавать не нужно

У Temporal UI (`8233`) и gRPC-фронтенда (`7233`) нет аутентификации. Доступайтесь
к ним через SSH-проброс портов или Tailscale:

```bash
ssh -L 8233:127.0.0.1:8233 user@host
```

`openhands` (порт `8000` на хосте) тоже держите приватным: он работает с
`DISABLE_AUTH=true` и открыл бы неаутентифицированный runtime агента.

---

## Шаг 3 — Basic Auth через Access List в NPM

Access List — это переиспользуемый набор учётных данных, который NPM
подставляет как `auth_basic` в сгенерированный vhost. Он защищает панель даже
если Cloudflare Access настроен с ошибкой или обойдён по IP оригинала.

1. Откройте админку NPM (`http://<host>:81`) и войдите.
2. **Access Lists → Add Access List**.
3. Заполните **Details**:
   - **Name**: `orchestrator-operators`
   - **Satisfy Any**: OFF (при *Any* запрос пропускается, если совпало *либо*
     IP-правило, *либо* учётные данные; для строгого гейта нужна семантика
     *All*).
4. Перейдите на вкладку **Authorization** → **Add Authorization**:
   - **Username**: `operator` (не `admin`)
   - **Password**: сгенерируйте длинный случайный, например
     `openssl rand -base64 24`
   - Нажмите **Save**. NPM хранит bcrypt-хеш, а не открытый пароль.
5. Не обязательно, но полезно — вкладка **Access**: добавьте правило *Allow* для
   CIDR офиса/VPN/Tailscale и `deny all` в конце.
6. **Save**, затем откройте proxy host `ui.example.com` → **Access List** →
   выберите `orchestrator-operators` → **Save**.

Проверка — без учётных данных origin обязан ответить `401`:

```bash
curl -sI https://ui.example.com/ | head -n 5
# HTTP/2 401
# www-authenticate: Basic realm="Authorization Required"

curl -sI -u operator:'<пароль>' https://ui.example.com/ | head -n 3
# HTTP/2 200
```

Применяйте тот же Access List к `api.example.com` **только если** ваша
автоматизация умеет передавать учётные данные. Иначе используйте service token
Cloudflare (шаг 8) и оставьте API-хост без Basic Auth, чтобы `curl` в CI не
хранил человеческий пароль.

> Браузер кеширует Basic Auth по realm и отправляет данные превентивно. При
> тестах двух аккаунтов используйте приватное окно, а сам realm всегда отдавайте
> по HTTPS — Basic Auth это base64, а не шифрование.

---

## Шаг 4 — Кастомный SSL-сертификат (StartSSL / Custom CRT + Key)

NPM может либо выпустить сертификат Let's Encrypt, либо принять готовую
PEM-пару. Кастомный путь нужен, когда сертификат выдан Cloudflare Origin CA,
внутренним CA или коммерческим/устаревшим удостоверяющим центром.

### 4.1 Получите пару

- **Cloudflare Origin CA (рекомендуется, если домен проксируется Cloudflare)**:
  панель Cloudflare → **SSL/TLS → Origin Server → Create Certificate**, хосты
  `*.example.com` + `example.com`, срок 15 лет. Вы получите origin-сертификат и
  приватный ключ. Edge Cloudflare ему доверяет, поэтому он работает с
  **Full (strict)**.
- **Let's Encrypt (HTTP-01)** внутри NPM — работает только когда порт `80`
  доходит до оригинала напрямую; если прокси Cloudflare включён, используйте
  **DNS-01** или Origin CA выше.
- **Готовый корпоративный/legacy CRT** — склейте листовой сертификат и
  промежуточные в один файл (`fullchain.pem`).

> ⚠️ **Про «StartSSL».** В старых сборках NPM есть пункт *StartSSL*, который
> ссылается на CA StartCom. Сертификаты StartCom не доверяются браузерами с
> 2016 года, поэтому используйте его только для внутреннего CA, которому ваши
> клиенты уже доверяют. В современных сборках NPM форма одна — **Custom**;
> шаги ниже описывают именно её, для legacy CRT процесс такой же.

### 4.2 Импорт в NPM

1. **SSL Certificates → Add SSL Certificate → Custom**.
2. **Name**: `example.com-wildcard`.
3. **Server Name**: `*.example.com` (или точный FQDN; должен совпадать с CN/SAN
   сертификата).
4. **Certificate**: вставьте **полную цепочку** — сначала листовой, затем
   промежуточные:

   ```text
   -----BEGIN CERTIFICATE-----
   ...leaf...
   -----END CERTIFICATE-----
   -----BEGIN CERTIFICATE-----
   ...intermediate...
   -----END CERTIFICATE-----
   ```

5. **Private Key**: вставьте ключ без пароля:

   ```text
   -----BEGIN PRIVATE KEY-----
   ...
   -----END PRIVATE KEY-----
   ```

6. **Intermediate Certificate**: необязательно — только если цепочку не
   добавляли в поле *Certificate*.
7. **Save**. NPM записывает файлы в
   `/data/custom_ssl/npm-<id>/{fullchain.pem,privkey.pem}` и перезагружает nginx.
8. В каждом proxy host → вкладка **SSL** → выберите сертификат → **Force SSL**
   ON → **Save**.

### 4.3 Проверка

```bash
openssl s_client -connect ui.example.com:443 -servername ui.example.com \
  -showcerts </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

Несовпадение **Server Name** с CN/SAN сертификата заставляет nginx отдать
default vhost — это выглядит как `ERR_CERT_COMMON_NAME_INVALID` или `526` от
Cloudflare.

> Если прокси Cloudflare включён, выставьте **SSL/TLS → Overview → Full
> (strict)** и следите за сроком origin-сертификата: режим *Flexible* вместе с
> *Force SSL* в NPM даёт бесконечный редирект.

---

## Шаг 5 — ⚠️ Включите `Websockets Support` для Streamlit

**Это самая частая причина, по которой полностью настроенный деплой выглядит
сломанным.** Streamlit не опрашивает сервер по HTTP: браузер открывает WebSocket
на `/_stcore/stream` и держит его всю сессию.

Если **Websockets Support** выключен на proxy host `ui.example.com`, NPM не
передаёт заголовки `Upgrade` / `Connection`, рукопожатие не проходит, страница
загружается, но остаётся на *«Please wait…»* / *«Connecting…»* и в итоге
показывает **«Connection error»**. В логах контейнера при этом пусто, потому что
запрос так и не стал валидным WebSocket — именно поэтому тумблер легко упустить.

Чек-лист:

1. `Hosts → Proxy Hosts → ui.example.com → Edit`.
2. Вкладка **Details** → **Websockets Support** → **ON**.
3. **Save** (nginx перезагрузится сам, перезапуск контейнера не нужен).
4. Жёсткая перезагрузка дашборда (`Ctrl+Shift+R`).

Проверка на уровне nginx — в сгенерированном vhost должны быть заголовки
upgrade:

```bash
docker exec nginx-proxy-manager sh -c \
  'grep -n -A2 "Upgrade" /data/nginx/proxy_host/*.conf'
# proxy_set_header Upgrade $http_upgrade;
# proxy_set_header Connection $http_connection;
```

Проверка рукопожатия целиком (ожидается `101 Switching Protocols`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' \
  -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==' \
  https://ui.example.com/_stcore/stream
# 101
```

`400` или `200` здесь означают, что заголовки upgrade всё ещё срезаются. Также
проверьте в браузере: **DevTools → Network → WS** — запись `_stcore/stream`
должна иметь статус `101`, а **Console** не должна содержать предупреждений о
переподключении.

Примечания:

- Cloudflare проксирует WebSocket прозрачно; отдельного тумблера там нет.
- Не ставьте правило кеширования на `ui.example.com/_stcore/*` — закешированный
  или буферизованный поток ломает сессию даже при включённом тумблере.
- API-хосту (`api.example.com`) WebSocket не нужен, оставьте OFF.

---

## Шаг 6 — Cloudflare Zero Trust: вход по Email PIN / OTP

1. Панель Cloudflare → **Zero Trust**. При первом входе задайте **имя команды**
   (оно станет `<team>.cloudflareaccess.com`) и выберите бесплатный план.
2. **Settings → Authentication → Login methods**. **One-time PIN** включён по
   умолчанию и не требует внешнего IdP — Cloudflare отправляет PIN на адрес,
   который вводят на странице входа. Позже здесь можно добавить Google/GitHub
   SSO.
3. **Settings → Custom pages** (необязательно): укажите имя команды и контакт на
   странице входа Access, чтобы операторы знали, к кому обращаться.
4. **Settings → WARP authentication** / регистрация устройств для браузерного
   деплоя не нужны.

> OTP подтверждает *кто* пользователь, и не более. Он не заменяет Basic Auth из
> шага 3: если Cloudflare Access когда-нибудь обойдут по IP оригинала, NPM всё
> равно спросит учётные данные.

---

## Шаг 7 — Access Application для UI (политика Allow)

1. **Access → Applications → Add an application → Self-hosted**.
2. **Application configuration**:
   - **Application name**: `agent-orchestrator UI`
   - **Session Duration**: `8 hours` (либо `24 hours` для долгого спринта;
     короче — безопаснее, потому что cookie сессии остаётся единственным
     барьером между интернетом и панелью, запускающей агентов).
3. **Public hostname**:
   - **Subdomain**: `ui`
   - **Domain**: `example.com`
   - **Path**: оставьте пустым (защищается весь хост)
4. **Access policies → Add a policy**:
   - **Policy name**: `operators`
   - **Action**: `Allow`
   - **Configure rules → Include → Emails** — перечислите адреса операторов либо
     используйте **Emails ending in** `@example.com` для всего корпоративного
     домена.
   - Необязательно: **Require → Country**, чтобы отсечь всё за пределами региона.
5. **Save**. Теперь неаутентифицированные запросы Access получают `302` на
   `https://<team>.cloudflareaccess.com/cdn-cgi/access/login/...`.

Повторите для второго приложения, если API-хост тоже нужно закрыть
идентификацией (почему обычно не через OTP — см. следующий шаг).

---

## Шаг 8 — Исключения (Bypass Rules) для `/api/*` и `/webhook/*`

Автоматизация не может ввести PIN. Есть два механизма, и применять их стоит к
разным путям:

| Путь | Механизм | Зачем |
|---|---|---|
| `/api/*` (например, `POST /api/v1/plan/direct`) | **Service Token** (предпочтительно) или **Bypass** | CI/curl должны вызывать gateway без человека |
| `/webhook/*` (GitHub, GitLab, мониторинг) | **Bypass** + проверка HMAC-подписи в приложении | У отправителя нет идентичности Cloudflare |

### 8.1 Почему для пути нужно отдельное Access-приложение

Политики Access привязаны к **приложению**, а не к пути внутри него, поэтому
bypass выражается отдельным приложением на этот путь. Cloudflare обрабатывает
самое специфичное совпадающее приложение первым, поэтому `api.example.com/api`
имеет приоритет над `api.example.com`.

1. **Access → Applications → Add an application → Self-hosted**.
2. **Application name**: `agent-orchestrator API`.
3. **Public hostname**: subdomain `api`, domain `example.com`,
   **Path**: `api` (сопоставление по пути в Cloudflare префиксное, поэтому
   `/api/v1/plan/direct` покрыт).
4. **Access policies → Add a policy**:
   - **Policy name**: `service-token-only`
   - **Action**: `Service Auth`
   - **Include → Service Token** → токен, созданный в 8.2
5. **Save**.

### 8.2 Создайте service token (рекомендуется для `/api/*`)

1. **Access → Service Auth → Service Tokens → Create Service Token**.
2. **Name**: `orchestrator-ci`, **Duration**: `1 year` (меняйте до истечения).
3. Скопируйте **Client ID** и **Client Secret** — секрет показывается один раз.
4. Храните их в секретах CI, никогда в репозитории:

```bash
export CF_ACCESS_CLIENT_ID='<client-id>.access'
export CF_ACCESS_CLIENT_SECRET='<client-secret>'

curl -s https://api.example.com/health \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
# {"status":"ok"}
```

Запросы без этих заголовков получат `302`/`403` от Cloudflare и не дойдут до
FastAPI.

### 8.3 Чистый bypass (для отправителей без заголовков)

Для GitHub webhook или сторонних вызывающих замените действие политики из 8.1
на:

- **Action**: `Bypass`
- **Include → Everyone**

Тогда приложение вообще не проверяет идентичность, поэтому **само приложение
обязано аутентифицировать вызывающего**. Для GitHub это проверка
`X-Hub-Signature-256` (HMAC-SHA256 по сырому телу с secret вебхука) и отказ
всему, что не совпало. Оставьте NPM Basic Auth на этом хосте вторым замком либо,
что лучше, ограничьте хост опубликованными диапазонами IP отправителя.

> ⚠️ **Проверьте область действия.** `agent-orchestrator-lite` отдаёт только
> `GET /health` и `POST /api/v1/plan/direct`; GitHub webhook из Lite-версии
> удалён. Создавайте bypass `/webhook/*` **только** если используете полную
> версию или собственный приёмник — неиспользуемое исключение это лишняя дыра.
> Никогда не делайте bypass шире нужного префикса и никогда не исключайте `/` на
> UI-хосте: этот хост управляет исполнением агентов.

### 8.4 Порядок проверок

```
Cloudflare Access (OTP для ui.example.com, токен/bypass для api.example.com)
        ↓
NPM Access List (Basic Auth, на каждый proxy host)
        ↓
FastAPI / Streamlit
```

Слои независимы. Отключение одного не отключает другой — в этом и смысл схемы.

---

## Шаг 9 — Сквозная проверка

Проходите матрицу сверху вниз: каждая строка проверяет один слой.

| # | Запрос | Ожидаемый результат | Проверяемый слой |
|---|---|---|---|
| 1 | `curl -sI https://ui.example.com/` | `302` → `<team>.cloudflareaccess.com` | Cloudflare Access |
| 2 | Тот же URL с валидной cookie `CF_Authorization` | `401` + `www-authenticate: Basic` | NPM Access List |
| 3 | Тот же URL с cookie **и** `-u operator:...` | `200` HTML | вся цепочка |
| 4 | `curl -s https://api.example.com/health` | `302`/`403` | API не публичный |
| 5 | То же с `CF-Access-Client-Id`/`-Secret` | `{"status":"ok"}` | service token |
| 6 | WebSocket-проба на `/_stcore/stream` | `101` | Websockets Support |
| 7 | `openssl s_client ... -servername ui.example.com` | issuer = ваш CA, сроки валидны | кастомный SSL |

Запуск плана через защищённый API:

```bash
curl -s -X POST https://api.example.com/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -d '{
        "raw_json": "{\"tasks\":[{\"id\":\"step_1\",\"title\":\"Hello\",\"prompt\":\"Create hello.txt containing hi\",\"depends_on\":[],\"engine\":\"openhands\"}]}",
        "auto_approve": true
      }'
# {"status":"triggered","workflow_id":"direct-plan-...","mode":"direct_json_plan",...}
```

Если строка 1 сразу возвращает `200`, Access-приложение не совпало с хостом
(проверьте public hostname, а не только политику). Если строка 2 возвращает
`200` без учётных данных, Access List не привязан к proxy host.

---

## Диагностика

| Симптом | Вероятная причина | Решение |
|---|---|---|
| UI загружается, но висит на «Please wait…» / «Connection error», логи контейнера чистые | `Websockets Support` выключен в NPM | Шаг 5 — включить тумблер, сохранить, жёстко перезагрузить |
| `502 Bad Gateway` от NPM | Неверный upstream: `127.0.0.1:8501` при контейнерном NPM либо `8085` внутри Docker-сети | Используйте `agent-ui:8501` / `agent-orchestrator:8000` или вариант B |
| `522` / `524` от Cloudflare | Origin недоступен, фаервол режет IP Cloudflare либо запрос длится дольше 100 с | Открыть `80`/`443` для диапазонов Cloudflare; длинные вызовы агентов держать на `/api/*` с увеличенным таймаутом прокси |
| `526` invalid SSL certificate | Cloudflare **Full (strict)**, а NPM отдаёт самоподписанный/просроченный сертификат | Импортировать Cloudflare Origin CA (шаг 4.1) или временно перейти на Full |
| Бесконечный редирект (`ERR_TOO_MANY_REDIRECTS`) | Cloudflare **Flexible** + **Force SSL** в NPM | Выставить режим **Full (strict)** |
| `403` со страницей Cloudflare Access | Email нет в политике Allow либо истёк OTP | Шаг 7, перепроверьте правило *Include* |
| Цикл запроса Basic Auth | Закешированы неверные данные либо `Satisfy Any` с несовпадающим IP-правилом | Проверьте в приватном окне, установите `Satisfy Any` OFF |
| `400` на пробе `/_stcore/stream` | Срезаны заголовки upgrade (WebSockets off) либо правило прокси/WAF переписывает путь | Шаг 5, затем проверьте правила Cloudflare для `_stcore` |
| Страница входа Cloudflare Access не появляется | DNS-запись не проксируется (серая тучка) или опечатка в hostname приложения | Включите оранжевую тучку, проверьте public hostname |
| `curl` работает, браузер получает `401` на каждый запрос | Cloudflare срезает/игнорирует `Authorization` на некоторых планах либо браузер закешировал старый realm | Для автоматизации используйте service token; очистите сохранённые данные realm |

Полезные логи:

```bash
docker logs --tail 100 nginx-proxy-manager
docker logs --tail 100 agent-orchestrator-lite
docker logs --tail 100 agent-ui
```

---

## Чек-лист безопасности

- [ ] `BIND_IP` — `127.0.0.1` (вариант A) либо приватный/Tailscale-адрес (вариант B), но никогда `0.0.0.0` на публичном хосте.
- [ ] Админка NPM (порт `81`) не опубликована наружу; дефолтные `admin@example.com` / `changeme` сразу изменены.
- [ ] `ui.example.com` закрыт Cloudflare Access **и** Access List в NPM.
- [ ] `api.example.com` требует service token (либо bypass + проверка подписи в приложении); `/api/v1/plan/direct` запускает агентов и не может быть анонимным.
- [ ] Исключены только `/api/*` (и `/webhook/*`, если он реально используется), но никогда `/`.
- [ ] Порты `7233`, `8233`, `8000` не опубликованы в интернет.
- [ ] TLS в режиме **Full (strict)**, origin-сертификат валиден, дата продления в календаре.
- [ ] `Websockets Support` включён для Streamlit-хоста и не попадает под правила кеширования.
- [ ] `GITHUB_TOKEN`, `LLM_API_KEY` и `OPENHANDS_SESSION_API_KEY` живут только в `.env`/секретах, не в плане, не в URL и не в логах.
- [ ] Для `/api/*` включён rate limiting Cloudflare WAF (запуск плана дорог).
- [ ] Сессии Access истекают (8–24 ч), у service token есть срок действия.

---

## Связанная документация

- [README.ru.md](../../README.ru.md) — быстрый старт, порты и формат JSON-плана.
- [Настройка OpenHands и LLM-провайдеров](../openhands-setup.ru.md) — контейнер
  `openhands` и ключи провайдеров.
- [AGENTS.ru.md](../../AGENTS.ru.md) — спецификация `ExecutionPlan`.
- [`docker-compose.yml`](../../docker-compose.yml) и
  [`.env.example`](../../.env.example) — порты, `BIND_IP`, имена сервисов.
- [English version](securing-with-npm-and-cloudflare.md)
