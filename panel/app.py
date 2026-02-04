import os
import subprocess
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
VPN_GROUP = "slipstream-users"
USER_PREFIX = "ss_"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_vpn_users():
    users = []
    try:
        # Get users in the specific group
        result = subprocess.run(['getent', 'group', VPN_GROUP], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            parts = result.stdout.strip().split(':')
            if len(parts) >= 4 and parts[3]:
                users = parts[3].split(',')
    except Exception as e:
        print(f"Error getting users: {e}")
    return sorted(users)

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

@app.route(f'/{PANEL_PATH}/add_user', methods=['POST'])
@login_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')

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

        flash(f'کاربر {username} با موفقیت ساخته شد', 'success')
    except Exception as e:
        flash(f'خطا در ساخت کاربر: {str(e)}', 'danger')

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
