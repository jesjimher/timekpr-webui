from flask import session, request
from src.database import db, Settings, ManagedUser


def get_auth_mode():
    """Get the current authentication mode from settings"""
    mode = Settings.get_value('AUTH_MODE', 'local')
    return mode


def get_authenticated_user():
    """
    Get the authenticated user based on the current auth mode.
    Returns True if authenticated, False otherwise.
    """
    auth_mode = get_auth_mode()

    if auth_mode == 'local':
        # Mode: local (session-based authentication with admin user)
        if session.get('logged_in'):
            return True
        return False

    elif auth_mode == 'external':
        # Mode: external (authentication delegated to reverse proxy)
        # Always return True - assume proxy has already authenticated
        session['logged_in'] = True
        return True

    return False


def login_required(f):
    """Decorator to require authentication for a route"""
    from functools import wraps
    from flask import redirect, url_for, flash

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            flash('Please login first', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def set_auth_mode(mode):
    """Set the authentication mode"""
    if mode not in ['local', 'external']:
        raise ValueError(f"Invalid auth mode: {mode}")
    Settings.set_value('AUTH_MODE', mode)
