import os
import subprocess
import sqlite3
import re
import threading
import contextlib
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
from functools import wraps
from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
csrf = CSRFProtect(app)

# Configuration
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'admin')
PANEL_PATH = os.environ.get('PANEL_PATH', 'panel')
DB_PATH = os.environ.get('DB_PATH', 'panel.db')
VPN_GROUP = "slipstream-users"
USER_PREFIX = "ss_"

@contextlib.contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with db_conn() as conn:
        with conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expiry_date DATETIME,
                    total_bytes BIGINT DEFAULT 0
                )
            ''')

init_db()

STATE_FILE = '/tmp/panel_log_pos'

def update_usage_from_logs():
    log_path = os.environ.get('DANTE_LOG', '/var/log/danted.log')
    if not os.path.exists(log_path):
        return

    try:
        current_pos = 0
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    current_pos = int(f.read().strip())
            except:
                current_pos = 0

        file_size = os.path.getsize(log_path)
        if file_size < current_pos:
            current_pos = 0 # Log rotated

        if file_size == current_pos:
            return

        lines = []
        new_pos = current_pos
        with open(log_path, 'r') as f:
            f.seek(current_pos)
            lines = f.readlines()
            new_pos = f.tell()

        usage_map = {} # username -> total_bytes
        # finished(1): tcp/connect [: 1.2.3.4.5678 8.8.8.8.80 (ali)]: 1024 bytes sent, 2048 bytes received
        pattern = r'finished.*\((\w+)\)\]: (\d+) bytes sent, (\d+) bytes received'

        for line in lines:
            match = re.search(pattern, line)
            if match:
                username = match.group(1)
                sent = int(match.group(2))
                received = int(match.group(3))
                usage_map[username] = usage_map.get(username, 0) + sent + received

        if usage_map:
            with db_conn() as conn:
                with conn:
                    for username, bytes_count in usage_map.items():
                        conn.execute('UPDATE users SET total_bytes = total_bytes + ? WHERE username = ?', (bytes_count, username))

        with open(STATE_FILE, 'w') as f:
            f.write(str(new_pos))

        # Clear log if it's getting too big (optional)
        if file_size > 20 * 1024 * 1024: # 20MB
             subprocess.run(['truncate', '-s', '0', log_path])
             with open(STATE_FILE, 'w') as f:
                 f.write("0")
    except Exception as e:
        print(f"Error updating usage: {e}")

def get_online_users():
    online_users = set()
    log_path = os.environ.get('DANTE_LOG', '/var/log/danted.log')
    if not os.path.exists(log_path):
        return online_users

    try:
        # Check active connections to port 1080
        res = subprocess.run(['ss', '-nt', 'state', 'established', '( sport = :1080 or dport = :1080 )'], capture_output=True, text=True)
        active_sessions = set()
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                remote = parts[4] # remote address:port
                active_sessions.add(remote.strip('[]'))

        if not active_sessions:
            return online_users

        # Map sessions back to users using recent logs
        # pass(1): tcp/connect [: 1.2.3.4.5678 8.8.8.8.80 (ali)]
        log_res = subprocess.run(['tail', '-n', '500', log_path], capture_output=True, text=True)
        for line in log_res.stdout.splitlines():
            if 'pass' in line:
                for sess in active_sessions:
                    # ss uses IP:PORT, danted logs use IP.PORT
                    # Also handle IPv6 which might have different formatting
                    sess_log_fmt = sess.replace(':', '.')
                    if sess_log_fmt in line:
                        match = re.search(r'\((\w+)\)\]', line)
                        if match:
                            online_users.add(match.group(1))
    except Exception as e:
        print(f"Error getting online users: {e}")
    return online_users

# Background task for usage
def usage_worker():
    while True:
        update_usage_from_logs()
        time.sleep(60) # every minute

# Start thread if not in testing
if os.environ.get('START_WORKER', 'true') == 'true':
    t = threading.Thread(target=usage_worker, daemon=True)
    t.start()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def format_bytes(size):
    # 2**10 = 1024
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

def get_vpn_users():
    online_users = get_online_users()
    system_users = []
    try:
        # Get users in the specific group
        result = subprocess.run(['getent', 'group', VPN_GROUP], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            parts = result.stdout.strip().split(':')
            if len(parts) >= 4 and parts[3]:
                system_users = parts[3].split(',')
    except Exception as e:
        print(f"Error getting users: {e}")

    # Sync system users to DB if they don't exist
    with db_conn() as conn:
        with conn:
            for username in system_users:
                conn.execute('INSERT OR IGNORE INTO users (username) VALUES (?)', (username,))

        # Get all users from DB
        users = conn.execute('SELECT * FROM users ORDER BY username').fetchall()

    # Check if DB user still exists in system and handle expiration
    final_users = []
    now = datetime.now()
    for user in users:
        u_dict = dict(user)
        if u_dict['username'] in system_users:
            # Check expiration
            if u_dict['expiry_date'] and u_dict['is_active']:
                expiry = datetime.strptime(u_dict['expiry_date'], '%Y-%m-%d %H:%M:%S.%f' if '.' in u_dict['expiry_date'] else '%Y-%m-%d %H:%M:%S')
                if now > expiry:
                    # Expired! Disable user
                    try:
                        subprocess.run(['usermod', '-L', u_dict['username']], check=True)
                        with db_conn() as conn:
                            with conn:
                                conn.execute('UPDATE users SET is_active = 0 WHERE username = ?', (u_dict['username'],))
                        u_dict['is_active'] = 0
                    except Exception as e:
                        print(f"Error disabling expired user {u_dict['username']}: {e}")

            u_dict['is_online'] = u_dict['username'] in online_users
            u_dict['formatted_usage'] = format_bytes(u_dict['total_bytes'])
            final_users.append(u_dict)
        else:
            # User was deleted from system manually, remove from DB
            with db_conn() as conn:
                with conn:
                    conn.execute('DELETE FROM users WHERE username = ?', (user['username'],))

    return final_users

@app.route(f'/{PANEL_PATH}/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USER and password == ADMIN_PASS:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        flash('نام کاربری یا رمز عبور اشتباه است', 'danger')
    return render_template('login.html', panel_path=PANEL_PATH)

@app.route(f'/{PANEL_PATH}/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route(f'/{PANEL_PATH}/')
@app.route(f'/{PANEL_PATH}/dashboard')
@login_required
def dashboard():
    users = get_vpn_users()
    return render_template('dashboard.html', users=users, panel_path=PANEL_PATH, prefix=USER_PREFIX)

@app.route(f'/{PANEL_PATH}/toggle_user/<username>', methods=['POST'])
@login_required
def toggle_user(username):
    with db_conn() as conn:
        user = conn.execute('SELECT is_active FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            flash('کاربر یافت نشد', 'danger')
            return redirect(url_for('dashboard'))

        new_status = not user['is_active']
        try:
            if new_status:
                # Unlock user
                subprocess.run(['usermod', '-U', username], check=True)
            else:
                # Lock user
                subprocess.run(['usermod', '-L', username], check=True)

            with conn:
                conn.execute('UPDATE users SET is_active = ? WHERE username = ?', (new_status, username))
            flash(f'وضعیت کاربر {username} تغییر کرد', 'success')
        except Exception as e:
            flash(f'خطا در تغییر وضعیت: {str(e)}', 'danger')

    return redirect(url_for('dashboard'))

@app.route(f'/{PANEL_PATH}/add_user', methods=['POST'])
@login_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')
    duration = request.form.get('duration', type=int)

    if not username or not password:
        flash('نام کاربری و رمز عبور الزامی است', 'warning')
        return redirect(url_for('dashboard'))

    if not username.startswith(USER_PREFIX):
        username = USER_PREFIX + username

    try:
        # Check if user exists
        check_user = subprocess.run(['id', username], capture_output=True)
        if check_user.returncode == 0:
            flash(f'کاربر {username} از قبل وجود دارد', 'danger')
            return redirect(url_for('dashboard'))

        # Create user
        subprocess.run(['useradd', '-r', '-s', '/bin/false', '-M', '-G', VPN_GROUP, username], check=True)
        # Set password
        process = subprocess.Popen(['chpasswd'], stdin=subprocess.PIPE, text=True)
        process.communicate(input=f'{username}:{password}')

        # Add to DB
        expiry_date = None
        if duration and duration > 0:
            expiry_date = datetime.now() + timedelta(days=duration)

        with db_conn() as conn:
            with conn:
                conn.execute('INSERT INTO users (username, expiry_date) VALUES (?, ?)', (username, expiry_date))

        flash(f'کاربر {username} با موفقیت ساخته شد', 'success')
    except Exception as e:
        flash(f'خطا در ساخت کاربر: {str(e)}', 'danger')

    return redirect(url_for('dashboard'))

@app.route(f'/{PANEL_PATH}/reset_usage/<username>', methods=['POST'])
@login_required
def reset_usage(username):
    with db_conn() as conn:
        with conn:
            conn.execute('UPDATE users SET total_bytes = 0 WHERE username = ?', (username,))
        flash(f'حجم مصرفی کاربر {username} صفر شد', 'success')
    return redirect(url_for('dashboard'))

@app.route(f'/{PANEL_PATH}/reset_time/<username>', methods=['POST'])
@login_required
def reset_time(username):
    duration = request.form.get('duration', type=int)
    if not duration or duration <= 0:
        flash('مدت زمان نامعتبر است', 'warning')
        return redirect(url_for('dashboard'))

    expiry_date = datetime.now() + timedelta(days=duration)
    with db_conn() as conn:
        with conn:
            conn.execute('UPDATE users SET expiry_date = ?, is_active = 1 WHERE username = ?', (expiry_date, username))
        # Ensure user is unlocked if they were expired
        try:
            subprocess.run(['usermod', '-U', username], check=True)
        except:
            pass
        flash(f'زمان کاربر {username} تمدید شد', 'success')
    return redirect(url_for('dashboard'))

@app.route(f'/{PANEL_PATH}/delete_user/<username>', methods=['POST'])
@login_required
def delete_user(username):
    if username == ADMIN_USER:
        flash('امکان حذف کاربر ادمین وجود ندارد', 'danger')
        return redirect(url_for('dashboard'))

    if not username.startswith(USER_PREFIX):
        flash('فقط کاربران وی‌پی‌ان قابل حذف هستند', 'danger')
        return redirect(url_for('dashboard'))

    try:
        subprocess.run(['userdel', username], check=True)
        flash(f'کاربر {username} با موفقیت حذف شد', 'success')
    except Exception as e:
        flash(f'خطا در حذف کاربر: {str(e)}', 'danger')

    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    # When running normally, it will use the environment variables for port
    port = int(os.environ.get('PANEL_PORT', 17066))
    app.run(host='0.0.0.0', port=port)
