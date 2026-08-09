"""
Shared Flask extension instances.

Kept in their own module (instead of inside the entry-point file) so that
both the auth blueprint and your main application file can import the
*same* db/login_manager objects without circular imports.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth
from flask_socketio import SocketIO

db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()

# Powers live Speed Battle matchmaking/rooms (battle.py). async_mode is
# left as the default ("threading" when eventlet/gevent aren't
# installed) - fine for the single dev-server process this app runs as;
# swap in eventlet/gevent + a real message_queue (Redis) before ever
# running this behind multiple worker processes, since the matchmaking
# queue and battle rooms in battle.py are in-memory / this-process-only.
socketio = SocketIO()
