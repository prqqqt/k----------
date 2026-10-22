import hashlib
import hmac
import json
import os
import secrets
import string
import urllib.error
import urllib.request

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import mysql.connector

app = Flask(__name__)

DB_CONFIG = {
    "host": "185.114.247.43",
    "port": 3306,
    "database": "sch688_vvedenie",
    "user": "sch688_vvedenie",
    "password": "Qwerty123",
}

# AI-провайдер: любой OpenAI-совместимый chat/completions endpoint.
# По умолчанию — локальный Ollama (см. ai/README.md), внешние сервисы можно
# подключить через переменные окружения AI_API_URL / AI_MODEL / AI_API_KEY.
AI_API_URL = os.environ.get(
    'AI_API_URL', 'http://127.0.0.1:11434/v1/chat/completions')
AI_MODEL = os.environ.get('AI_MODEL', 'qwen2.5:1.5b')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_TIMEOUT = int(os.environ.get('AI_TIMEOUT', '120'))

AI_SYSTEM_PROMPT = (
    'Ты дружелюбный и полезный ассистент внутри учебного Flask-приложения. '
    'Отвечай на языке пользователя, кратко и по делу.'
)

# Стоимость одного запроса к нейросети (запасное значение, если настройки
# в базе недоступны). Основной источник — settings.ai_price.
AI_PRICE_DEFAULT = 1

TRANSACTION_TYPES = {
    'initial': 'Бонус',
    'ai_request': 'AI-запрос',
    'refund': 'Возврат',
    'deposit': 'Пополнение',
    'withdraw': 'Списание',
    'promo': 'Промокод',
}


class InsufficientFunds(Exception):
    """На балансе недостаточно денег для оплаты запроса."""

    def __init__(self, balance, price):
        self.balance = balance
        self.price = price
        super().__init__(
            'Недостаточно средств: запрос стоит %d ₽, на балансе %d ₽' % (price, balance))


def load_secret_key():
    """Ключ для подписи cookie-сессий: из окружения или из файла рядом с app.py."""
    key = os.environ.get('SECRET_KEY')
    if key:
        return key

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as fh:
            key = fh.read().strip()
        if key:
            return key

    key = secrets.token_hex(32)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


app.secret_key = load_secret_key()


def connect_db():
    return mysql.connector.connect(**DB_CONFIG)


def get_payload():
    """Данные из JSON-запроса (AJAX) или из обычной HTML-формы."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def hash_password(password):
    """Новый пароль всегда сохраняем только в виде хеша (werkzeug scrypt)."""
    return generate_password_hash(password)


def is_hashed(value):
    """Похоже ли значение на хеш, а не на пароль в открытом виде."""
    if value.startswith(('scrypt:', 'pbkdf2:', 'argon2')):
        return True
    # старый формат этой базы: unsalted SHA-256 в hex
    return len(value) == 64 and all(ch in string.hexdigits for ch in value)


def needs_rehash(value):
    """Всё, что не scrypt, при следующем входе переводим на актуальный формат."""
    return not value.startswith('scrypt:')


def verify_password(stored, password):
    """Сверяет пароль с сохранённым значением, поддерживая старые форматы."""
    if not stored:
        return False

    if stored.startswith(('scrypt:', 'pbkdf2:', 'argon2')):
        try:
            return check_password_hash(stored, password)
        except (ValueError, TypeError, ImportError):
            return False
    if len(stored) == 64 and all(ch in string.hexdigits for ch in stored):
        digest = hashlib.sha256(password.encode('utf-8')).hexdigest()
        return hmac.compare_digest(digest, stored.lower())

    # остался пароль в открытом виде (старые записи)
    return hmac.compare_digest(stored.encode('utf-8'), password.encode('utf-8'))


def ask_ai(messages):
    """Отправляет переписку в OpenAI-совместимый API и возвращает текст ответа."""
    payload = json.dumps({'model': AI_MODEL, 'messages': messages}).encode('utf-8')
    req = urllib.request.Request(
        AI_API_URL, data=payload, method='POST',
        headers={'Content-Type': 'application/json'})
    if AI_API_KEY:
        req.add_header('Authorization', 'Bearer ' + AI_API_KEY)

    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError('AI-сервис вернул ошибку %s' % exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError(
            'Не удалось связаться с AI-сервисом (%s). '
            'Проверьте, что он запущен' % AI_API_URL)

    choices = body.get('choices') or []
    reply = (choices[0].get('message') or {}).get('content') if choices else None
    if not reply:
        raise RuntimeError('AI-сервис вернул пустой ответ')

    return reply.strip()


def save_chat_message(user_id, role, content):
    cnx = connect_db()
    try:
        cur = cnx.cursor()
        cur.execute(
            'INSERT INTO `chat_messages`(`user_id`, `role`, `content`) VALUES (%s, %s, %s)',
            (user_id, role, content))
        cnx.commit()
    finally:
        cnx.close()


def load_chat_history(user_id, limit=50):
    """Последние сообщения пользователя в хронологическом порядке."""
    cnx = connect_db()
    try:
        cur = cnx.cursor(dictionary=True)
        cur.execute(
            'SELECT `role`, `content`, `created_at` FROM `chat_messages` '
            'WHERE `user_id` = %s ORDER BY `id` DESC LIMIT %s',
            (user_id, limit))
        rows = cur.fetchall()
    finally:
        cnx.close()

    rows.reverse()
    return rows


def get_ai_price():
    """Стоимость запроса из settings.ai_price (по умолчанию 1 ₽)."""
    try:
        cnx = connect_db()
        try:
            cur = cnx.cursor()
            cur.execute('SELECT `value` FROM `settings` WHERE `key` = %s', ('ai_price',))
            row = cur.fetchone()
        finally:
            cnx.close()
    except mysql.connector.Error:
        app.logger.exception('Не удалось прочитать settings.ai_price')
        return AI_PRICE_DEFAULT

    if not row:
        return AI_PRICE_DEFAULT

    try:
        price = int(float(row[0]))
    except (TypeError, ValueError):
        return AI_PRICE_DEFAULT

    return price if price > 0 else AI_PRICE_DEFAULT


def fetch_balance(user_id):
    cnx = connect_db()
    try:
        cur = cnx.cursor()
        cur.execute('SELECT `balance` FROM `users` WHERE `id` = %s', (user_id,))
        row = cur.fetchone()
    finally:
        cnx.close()

    return (row[0] or 0) if row else 0


def charge_for_ai(user_id, price):
    """Атомарно списывает price ₽ и пишет транзакцию. Возвращает новый баланс.

    Строка пользователя блокируется (SELECT ... FOR UPDATE), поэтому два
    одновременных запроса не смогут потратить одни и те же деньги.
    """
    cnx = connect_db()
    try:
        cnx.start_transaction()
        cur = cnx.cursor()

        cur.execute('SELECT `balance` FROM `users` WHERE `id` = %s FOR UPDATE',
                    (user_id,))
        row = cur.fetchone()
        if row is None:
            cnx.rollback()
            raise mysql.connector.Error(msg='Пользователь не найден')

        balance = row[0] or 0
        if balance < price:
            cnx.rollback()
            raise InsufficientFunds(balance, price)

        new_balance = balance - price
        cur.execute(
            'UPDATE `users` SET `balance` = %s, '
            '`total_spent` = COALESCE(`total_spent`, 0) + %s WHERE `id` = %s',
            (new_balance, price, user_id))
        cur.execute(
            'INSERT INTO `transactions`(`user_id`, `amount`, `type`, `cost`, '
            '`description`, `balance_after`) VALUES (%s, %s, %s, %s, %s, %s)',
            (user_id, -price, 'ai_request', 0, 'Запрос к нейросети', new_balance))
        cnx.commit()
        return new_balance
    finally:
        cnx.close()


def refund_ai_request(user_id, price):
    """Возвращает деньги, если запрос не удался после списания."""
    try:
        cnx = connect_db()
        try:
            cnx.start_transaction()
            cur = cnx.cursor()

            cur.execute('SELECT `balance` FROM `users` WHERE `id` = %s FOR UPDATE',
                        (user_id,))
            row = cur.fetchone()
            if row is None:
                cnx.rollback()
                return

            new_balance = (row[0] or 0) + price
            cur.execute(
                'UPDATE `users` SET `balance` = %s, '
                '`total_spent` = GREATEST(COALESCE(`total_spent`, 0) - %s, 0) '
                'WHERE `id` = %s',
                (new_balance, price, user_id))
            cur.execute(
                'INSERT INTO `transactions`(`user_id`, `amount`, `type`, `cost`, '
                '`description`, `balance_after`) VALUES (%s, %s, %s, %s, %s, %s)',
                (user_id, price, 'refund', 0,
                 'Возврат за неудачный запрос к нейросети', new_balance))
            cnx.commit()
        finally:
            cnx.close()
    except mysql.connector.Error:
        app.logger.exception('Не удалось вернуть средства за запрос')


@app.route('/user_register', methods=['POST'])
def user_register():
    req = get_payload()

    name = (req.get('name') or req.get('fullname') or '').strip()
    login = (req.get('email') or '').strip()
    password = req.get('password') or ''

    if not name or not login or not password:
        return jsonify(ok=False, error='Заполните все поля'), 400

    try:
        cnx = connect_db()
        try:
            cur = cnx.cursor()
            cur.execute(
                'INSERT INTO `users`(`username`, `email`, `password_hash`) VALUES (%s, %s, %s)',
                (name, login, hash_password(password)))
            cnx.commit()
            user_id = cur.lastrowid
        finally:
            cnx.close()
    except mysql.connector.IntegrityError:
        return jsonify(ok=False, error='Пользователь с таким email уже существует'), 409
    except mysql.connector.Error as exc:
        app.logger.exception('DB error on register')
        return jsonify(ok=False, error='Ошибка базы данных: %s' % exc), 503

    # после регистрации сразу входим и открываем профиль
    session.clear()
    session['user_id'] = user_id

    if request.is_json:
        return jsonify(ok=True, redirect=url_for('profile'))
    return redirect(url_for('profile'))


@app.route("/")
def registration():
    return render_template('registration.html')


@app.route("/login", methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if session.get('user_id'):
            return redirect(url_for('profile'))
        return render_template('login.html')

    req = get_payload()
    login_value = (req.get('username') or req.get('email') or req.get('login') or '').strip()
    password = req.get('password') or ''

    error = None
    user = None

    if not login_value or not password:
        error = 'Введите логин и пароль'
    else:
        row = None
        try:
            cnx = connect_db()
            try:
                cur = cnx.cursor()
                cur.execute(
                    'SELECT `id`, `username`, `email`, `password_hash` FROM `users` '
                    'WHERE `email` = %s OR `username` = %s '
                    'LIMIT 1',
                    (login_value, login_value))
                row = cur.fetchone()
            finally:
                cnx.close()
        except mysql.connector.Error as exc:
            app.logger.exception('DB error on login')
            error = 'Ошибка базы данных: %s' % exc

        if row is not None and verify_password(row[3], password):
            user = (row[1], row[2])
            if needs_rehash(row[3]):
                # старый пароль (SHA-256 или открытый текст) — незаметно
                # переводим его на современный хеш
                try:
                    cnx = connect_db()
                    try:
                        cur = cnx.cursor()
                        cur.execute(
                            'UPDATE `users` SET `password_hash` = %s WHERE `email` = %s',
                            (hash_password(password), row[2]))
                        cnx.commit()
                    finally:
                        cnx.close()
                except mysql.connector.Error:
                    app.logger.exception('Не удалось обновить хеш пароля')

    if user is not None:
        session.clear()
        session['user_id'] = row[0]
        if request.is_json:
            return jsonify(ok=True, username=user[0], email=user[1],
                           redirect=url_for('profile'))
        return redirect(url_for('profile'))

    if error is None:
        error = 'Неверный логин или пароль'

    if request.is_json:
        return jsonify(ok=False, error=error), 401
    return render_template('login.html', error=error, entered=login_value), 401


@app.route('/profile')
def profile():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    try:
        cnx = connect_db()
        try:
            cur = cnx.cursor(dictionary=True)
            cur.execute(
                'SELECT `id`, `username`, `email`, `balance`, `total_spent`, '
                '`total_deposited`, `created_at`, `is_admin`, '
                '`first_name`, `last_name` '
                'FROM `users` WHERE `id` = %s',
                (user_id,))
            user = cur.fetchone()
        finally:
            cnx.close()
    except mysql.connector.Error as exc:
        app.logger.exception('DB error on profile')
        return render_template('profile.html', user=None,
                               error='Ошибка базы данных: %s' % exc), 503

    if user is None:
        session.clear()
        return redirect(url_for('login'))

    return render_template('profile.html', user=user)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/transactions')
def transactions():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    rows = []
    error = None
    try:
        cnx = connect_db()
        try:
            cur = cnx.cursor(dictionary=True)
            cur.execute(
                'SELECT `id`, `amount`, `type`, `cost`, `description`, '
                '`balance_after`, `created_at` FROM `transactions` '
                'WHERE `user_id` = %s ORDER BY `created_at` DESC, `id` DESC LIMIT 200',
                (user_id,))
            rows = cur.fetchall()
        finally:
            cnx.close()
    except mysql.connector.Error as exc:
        app.logger.exception('DB error on transactions')
        error = 'Ошибка базы данных: %s' % exc

    for row in rows:
        row['type_label'] = TRANSACTION_TYPES.get(row['type'], row['type'])

    income = sum(r['amount'] for r in rows if (r['amount'] or 0) > 0)
    expense = -sum(r['amount'] for r in rows if (r['amount'] or 0) < 0)

    return render_template('transactions.html', rows=rows, income=income,
                           expense=expense, error=error)


@app.route('/ai')
def ai_chat():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    messages = []
    error = None
    balance = 0
    price = get_ai_price()
    try:
        messages = load_chat_history(user_id)
        balance = fetch_balance(user_id)
    except mysql.connector.Error as exc:
        app.logger.exception('DB error on chat history')
        error = 'Ошибка базы данных: %s' % exc

    return render_template('ai.html', messages=messages, error=error,
                           balance=balance, price=price)


@app.route('/ai/send', methods=['POST'])
def ai_send():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(ok=False, error='Нужно войти в аккаунт'), 401

    req = get_payload()
    text = (req.get('message') or '').strip()

    if not text:
        return jsonify(ok=False, error='Введите сообщение'), 400
    if len(text) > 4000:
        return jsonify(ok=False, error='Сообщение слишком длинное (максимум 4000 символов)'), 400

    price = get_ai_price()

    # Списываем деньги до обращения к нейросети: так два одновременных
    # запроса не смогут уйти в минус.
    try:
        balance = charge_for_ai(user_id, price)
    except InsufficientFunds as exc:
        return jsonify(ok=False, balance=exc.balance, price=exc.price,
                       error=str(exc)), 402
    except mysql.connector.Error as exc:
        app.logger.exception('DB error on charge')
        return jsonify(ok=False, error='Ошибка базы данных при списании средств: %s' % exc), 503

    try:
        save_chat_message(user_id, 'user', text)

        context = [{'role': 'system', 'content': AI_SYSTEM_PROMPT}]
        context += [{'role': m['role'], 'content': m['content'] or ''}
                    for m in load_chat_history(user_id, limit=12)
                    if m['role'] in ('user', 'assistant')]

        reply = ask_ai(context)
        save_chat_message(user_id, 'assistant', reply)
    except mysql.connector.Error as exc:
        app.logger.exception('DB error on AI send')
        refund_ai_request(user_id, price)
        return jsonify(ok=False, balance=fetch_balance(user_id), price=price,
                       error='Ошибка базы данных: %s' % exc), 503
    except RuntimeError as exc:
        app.logger.warning('AI error: %s', exc)
        refund_ai_request(user_id, price)
        return jsonify(ok=False, balance=fetch_balance(user_id), price=price,
                       error=str(exc)), 502
    except Exception:
        app.logger.exception('Unexpected error on AI send')
        refund_ai_request(user_id, price)
        return jsonify(ok=False, balance=fetch_balance(user_id), price=price,
                       error='Внутренняя ошибка при обращении к AI-сервису'), 502

    return jsonify(ok=True, reply=reply, balance=balance, price=price)


@app.cli.command('hash-passwords')
def hash_passwords_command():
    """Одноразовая миграция: хеширует пароли, сохранённые открытым текстом.

    Запуск: uv run flask --app app hash-passwords
    """
    cnx = connect_db()
    try:
        cur = cnx.cursor()
        cur.execute('SELECT `email`, `password_hash` FROM `users`')
        rows = cur.fetchall()

        upd = cnx.cursor()
        updated = 0
        for email, stored in rows:
            if not stored or is_hashed(stored):
                continue
            upd.execute(
                'UPDATE `users` SET `password_hash` = %s WHERE `email` = %s',
                (hash_password(stored), email))
            updated += 1
        cnx.commit()
    finally:
        cnx.close()

    print('Обновлено паролей: %d' % updated)


if __name__ == '__main__':
    app.run()
