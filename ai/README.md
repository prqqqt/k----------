# Локальный AI (Ollama)

Приложение по умолчанию обращается к локальному серверу Ollama:

```
AI_API_URL = http://127.0.0.1:11434/v1/chat/completions
AI_MODEL   = qwen2.5:1.5b
```

Внешний сервис не используется — запросы не уходят в интернет.

## Что установлено

- Ollama 0.40.8: `~/.local/opt/ollama` (симлинк `~/.local/bin/ollama`)
- Модель `qwen2.5:1.5b`: `~/.local/share/ollama/models`
- Пользовательская служба systemd: `~/.config/systemd/user/ollama.service`

## Управление

```bash
# статус
systemctl --user status ollama

# перезапуск / остановка
systemctl --user restart ollama
systemctl --user stop ollama

# список моделей
ollama list

# скачать другую модель
ollama pull qwen2.5:3b
```

Служба включена в автозапуск при входе в систему. Чтобы сервер работал и без
входа в графическую сессию (после перезагрузки), один раз выполните:

```bash
loginctl enable-linger "$USER"
```

## Другая модель

Скорость на этом компьютере (CPU, без видеокарты) зависит от размера модели.
`qwen2.5:1.5b` — быстрый вариант; для более качественных ответов можно взять
`qwen2.5:3b` (~2 ГБ) и указать её при запуске приложения:

```bash
ollama pull qwen2.5:3b
AI_MODEL=qwen2.5:3b uv run app.py
```

## Проверка

```bash
curl -s http://127.0.0.1:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:1.5b","messages":[{"role":"user","content":"Привет"}]}'
```

## Вернуться к внешнему сервису

```bash
AI_API_URL=https://text.pollinations.ai/openai AI_MODEL=openai uv run app.py
```
