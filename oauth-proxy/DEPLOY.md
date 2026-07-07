# Деплой oauth-proxy на Vercel (делается один раз тобой)

## 1. Установи Vercel CLI

```bash
npm install -g vercel
```

## 2. Задеплой

```bash
cd oauth-proxy
vercel --prod
```

Vercel спросит несколько вопросов — соглашайся на дефолты.
В конце выдаст URL вида `https://ticktick-mcp-oauth-xxxx.vercel.app`.

## 3. Задай переменные окружения в Vercel

```bash
vercel env add TICKTICK_CLIENT_ID production
vercel env add TICKTICK_CLIENT_SECRET production
vercel env add REDIRECT_URI production
```

Для `REDIRECT_URI` введи:
```
https://<твой-vercel-домен>/callback
```

Пример: `https://ticktick-mcp-oauth.vercel.app/callback`

Затем передеплой чтобы переменные подхватились:
```bash
vercel --prod
```

## 4. Добавь Redirect URI в TickTick developer app

1. Зайди на [developer.ticktick.com](https://developer.ticktick.com)
2. Открой своё приложение
3. В поле **OAuth Redirect URLs** добавь:
   ```
   https://<твой-vercel-домен>/callback
   ```
4. Сохрани

## 5. Проверь

Открой в браузере: `https://<твой-vercel-домен>/start`

Должна открыться страница входа TickTick. После логина — страница с токенами.

## Итог

Ссылка `/start` — это то, что ты даёшь людям вместо CLIENT_ID/SECRET.
Они логинятся своим TickTick, получают свои токены, твои credentials не видят.
