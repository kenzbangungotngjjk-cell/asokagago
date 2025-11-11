import os
import logging
import asyncio
import sqlite3
import json
import threading
import time
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from flask import Flask, jsonify, request
import subprocess
import shutil
import aiohttp
from threading import Thread
import re
import sys

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app for health checks
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "active", "service": "Telegram Bot Hosting", "timestamp": datetime.now().isoformat()})

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy", 
        "timestamp": datetime.now().isoformat(),
        "service": "Bot Hosting Service",
        "uptime": get_uptime()
    })

@app.route('/ping')
def ping():
    return jsonify({
        "status": "pong", 
        "timestamp": datetime.now().isoformat(),
        "message": "Service is alive and responding"
    })

@app.route('/stats')
def stats():
    return jsonify({
        "active_bots": DatabaseManager.get_active_bots_count(),
        "total_users": DatabaseManager.get_total_users(),
        "total_deployments": DatabaseManager.get_total_deployments(),
        "uptime": get_uptime()
    })

def run_flask_app():
    """Run Flask app for health checks"""
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

def start_health_server():
    """Start health check server in a separate thread"""
    flask_thread = Thread(target=run_flask_app, daemon=True)
    flask_thread.start()
    logger.info(f"Health check server started on port {os.getenv('PORT', 5000)}")

def get_uptime():
    """Calculate uptime for health checks"""
    if not hasattr(get_uptime, 'start_time'):
        get_uptime.start_time = datetime.now()
    uptime = datetime.now() - get_uptime.start_time
    return str(uptime).split('.')[0]  # Remove microseconds

def get_app_url():
    """Automatically detect the app URL"""
    # Try to get from Render's environment variables first
    render_external_url = os.getenv('RENDER_EXTERNAL_URL')
    if render_external_url:
        return render_external_url
    
    # Fallback to RENDER_APP_URL
    render_app_url = os.getenv('RENDER_APP_URL')
    if render_app_url:
        return render_app_url
    
    # Try to get from request context (when running)
    try:
        if request and request.host_url:
            return request.host_url.rstrip('/')
    except:
        pass
    
    # Fallback to localhost
    port = os.getenv('PORT', 5000)
    return f"http://localhost:{port}"

class DatabaseManager:
    @staticmethod
    def init_database():
        """Initialize SQLite database"""
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        # Users table with user tier
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                is_admin BOOLEAN DEFAULT FALSE,
                user_tier TEXT DEFAULT 'regular',  -- regular, premium
                max_bots INTEGER DEFAULT 1,        -- 1 for regular, 20 for premium
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Bots table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                bot_token TEXT,
                bot_username TEXT,
                deployment_path TEXT,
                is_active BOOLEAN DEFAULT FALSE,
                restart_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                last_ping TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'deployed',
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        # Payments table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                transaction_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        # System stats table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_deployments INTEGER DEFAULT 0,
                active_bots INTEGER DEFAULT 0,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Keep alive logs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keep_alive_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ping_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT,
                response_time REAL
            )
        ''')
        
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_user_tier(user_id: int) -> dict:
        """Get user tier and bot limits"""
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT user_tier, max_bots, is_admin FROM users WHERE id = ?
        ''', (user_id,))
        
        result = cursor.fetchone()
        
        if not result:
            # Create user with default regular tier
            cursor.execute('''
                INSERT INTO users (id, user_tier, max_bots) VALUES (?, ?, ?)
            ''', (user_id, 'regular', 1))
            conn.commit()
            user_tier = 'regular'
            max_bots = 1
            is_admin = False
        else:
            user_tier = result[0]
            max_bots = result[1]
            is_admin = bool(result[2])
        
        conn.close()
        
        return {
            'tier': user_tier,
            'max_bots': max_bots,
            'is_admin': is_admin
        }
    
    @staticmethod
    def get_user_bot_count(user_id: int) -> int:
        """Get number of bots deployed by user"""
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM bots WHERE user_id = ?', (user_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    @staticmethod
    def upgrade_user_tier(user_id: int, tier: str) -> bool:
        """Upgrade user to premium tier"""
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        try:
            if tier == 'premium':
                cursor.execute('''
                    UPDATE users SET user_tier = ?, max_bots = 20 WHERE id = ?
                ''', (tier, user_id))
                conn.commit()
                return True
            elif tier == 'regular':
                cursor.execute('''
                    UPDATE users SET user_tier = ?, max_bots = 1 WHERE id = ?
                ''', (tier, user_id))
                conn.commit()
                return True
            return False
        except Exception as e:
            logger.error(f"Error upgrading user tier: {e}")
            return False
        finally:
            conn.close()
    
    @staticmethod
    def add_bot_deployment(user_id: int, bot_token: str, bot_username: str, deployment_path: str, expires_at: datetime) -> int:
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO bots (user_id, bot_token, bot_username, deployment_path, expires_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, bot_token, bot_username, deployment_path, expires_at))
        
        bot_id = cursor.lastrowid
        
        # Update stats
        cursor.execute('''
            INSERT INTO system_stats (total_deployments, active_bots)
            VALUES (1, 1)
        ''')
        
        conn.commit()
        conn.close()
        return bot_id
    
    @staticmethod
    def get_user_bots(user_id: int):
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, bot_username, is_active, restart_count, 
                   created_at, expires_at, last_ping, status
            FROM bots 
            WHERE user_id = ?
            ORDER BY created_at DESC
        ''', (user_id,))
        
        bots = []
        for row in cursor.fetchall():
            bots.append({
                'id': row[0],
                'user_id': row[1],
                'bot_username': row[2],
                'is_active': bool(row[3]),
                'restart_count': row[4],
                'created_at': row[5],
                'expires_at': row[6],
                'last_ping': row[7],
                'status': row[8]
            })
        
        conn.close()
        return bots
    
    @staticmethod
    def get_bot_info(bot_id: int):
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, bot_token, bot_username, is_active, restart_count, 
                   created_at, expires_at, last_ping, status
            FROM bots 
            WHERE id = ?
        ''', (bot_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            'id': row[0],
            'user_id': row[1],
            'bot_token': row[2],
            'bot_username': row[3],
            'is_active': bool(row[4]),
            'restart_count': row[5],
            'created_at': row[6],
            'expires_at': row[7],
            'last_ping': row[8],
            'status': row[9]
        }
    
    @staticmethod
    def update_bot_status(bot_id: int, status: str, is_active: bool = None):
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        if is_active is not None:
            cursor.execute('''
                UPDATE bots SET status = ?, is_active = ?, last_ping = ? 
                WHERE id = ?
            ''', (status, is_active, datetime.now(), bot_id))
        else:
            cursor.execute('''
                UPDATE bots SET status = ?, last_ping = ? 
                WHERE id = ?
            ''', (status, datetime.now(), bot_id))
        
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_active_bots_count():
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM bots WHERE is_active = TRUE')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    @staticmethod
    def get_total_users():
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(DISTINCT user_id) FROM bots')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    @staticmethod
    def get_total_deployments():
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM bots')
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    @staticmethod
    def log_keep_alive(status: str, response_time: float):
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO keep_alive_logs (status, response_time)
            VALUES (?, ?)
        ''', (status, response_time))
        
        # Keep only last 100 logs to prevent DB bloat
        cursor.execute('''
            DELETE FROM keep_alive_logs 
            WHERE id NOT IN (
                SELECT id FROM keep_alive_logs 
                ORDER BY id DESC LIMIT 100
            )
        ''')
        
        conn.commit()
        conn.close()

class PaymentManager:
    @staticmethod
    def get_user_balance(user_id: int) -> int:
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            cursor.execute('INSERT INTO users (id, balance) VALUES (?, ?)', (user_id, 0))
            conn.commit()
            balance = 0
        else:
            balance = result[0]
        
        conn.close()
        return balance
    
    @staticmethod
    def deduct_stars(user_id: int, amount: int) -> bool:
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            
            if not result or result[0] < amount:
                return False
            
            cursor.execute(
                'UPDATE users SET balance = balance - ? WHERE id = ?',
                (amount, user_id)
            )
            
            cursor.execute(
                'INSERT INTO payments (user_id, amount, transaction_type) VALUES (?, ?, ?)',
                (user_id, amount, 'deployment')
            )
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"Payment deduction error: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    @staticmethod
    def add_stars(user_id: int, amount: int):
        conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                'UPDATE users SET balance = balance + ? WHERE id = ?',
                (amount, user_id)
            )
            
            cursor.execute(
                'INSERT INTO payments (user_id, amount, transaction_type) VALUES (?, ?, ?)',
                (user_id, amount, 'deposit')
            )
            
            conn.commit()
            
        except Exception as e:
            logger.error(f"Add stars error: {e}")
            conn.rollback()
        finally:
            conn.close()

class KeepAliveSystem:
    def __init__(self):
        self.is_running = True
        self.ping_count = 0
        
    async def start(self):
        """Start keep-alive system with 4-minute intervals"""
        logger.info("🚀 Starting keep-alive system with 4-minute intervals...")
        
        while self.is_running:
            try:
                self.ping_count += 1
                start_time = time.time()
                
                # Get app URL automatically
                app_url = get_app_url()
                
                # Ping health endpoint to keep Render awake
                try:
                    response = requests.get(f'{app_url}/health', timeout=10)
                    response_time = round((time.time() - start_time) * 1000, 2)
                    
                    if response.status_code == 200:
                        status = "success"
                        logger.info(f"✅ Keep-alive ping #{self.ping_count} to {app_url} - {response_time}ms")
                    else:
                        status = f"http_error_{response.status_code}"
                        logger.warning(f"⚠️ Keep-alive ping #{self.ping_count} HTTP error: {response.status_code}")
                    
                    DatabaseManager.log_keep_alive(status, response_time)
                    
                except requests.exceptions.RequestException as e:
                    response_time = round((time.time() - start_time) * 1000, 2)
                    status = f"error_{type(e).__name__}"
                    logger.warning(f"⚠️ Keep-alive ping #{self.ping_count} failed: {e}")
                    DatabaseManager.log_keep_alive(status, response_time)
                
                # Clean up expired bots every 12 pings (every 48 minutes)
                if self.ping_count % 12 == 0:
                    self.cleanup_expired_bots()
                
                # Update bot statuses every 6 pings (every 24 minutes)
                if self.ping_count % 6 == 0:
                    self.update_bot_statuses()
                
                # Log summary every 10 pings (every 40 minutes)
                if self.ping_count % 10 == 0:
                    self.log_system_summary()
                
                # Wait for 4 minutes before next ping (240 seconds)
                logger.debug(f"⏰ Waiting 4 minutes until next ping...")
                await asyncio.sleep(240)  # 4 minutes
                
            except Exception as e:
                logger.error(f"❌ Keep-alive system error: {e}")
                await asyncio.sleep(60)  # Wait 1 minute on error
    
    def cleanup_expired_bots(self):
        """Clean up expired bot deployments"""
        try:
            conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
            cursor = conn.cursor()
            
            cursor.execute(
                'DELETE FROM bots WHERE expires_at < ?',
                (datetime.now(),)
            )
            
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"🧹 Cleaned up {deleted_count} expired bots")
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"❌ Cleanup error: {e}")
    
    def update_bot_statuses(self):
        """Update bot statuses based on last ping"""
        try:
            conn = sqlite3.connect('bot_hosting.db', check_same_thread=False)
            cursor = conn.cursor()
            
            # Mark bots as inactive if no ping in last 10 minutes
            ten_min_ago = datetime.now() - timedelta(minutes=10)
            cursor.execute(
                'UPDATE bots SET is_active = FALSE WHERE last_ping < ?',
                (ten_min_ago,)
            )
            
            updated_count = cursor.rowcount
            if updated_count > 0:
                logger.info(f"🔄 Updated {updated_count} bots to inactive")
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"❌ Status update error: {e}")
    
    def log_system_summary(self):
        """Log system summary periodically"""
        active_bots = DatabaseManager.get_active_bots_count()
        total_users = DatabaseManager.get_total_users()
        total_deployments = DatabaseManager.get_total_deployments()
        
        logger.info(f"📊 System Summary - Bots: {active_bots}, Users: {total_users}, Deployments: {total_deployments}")

class BotProcessor:
    @staticmethod
    def extract_bot_token(python_code: str) -> tuple:
        """Extract bot token from Python code using multiple methods"""
        # Method 1: Look for common patterns
        patterns = [
            r'BOT_TOKEN\s*=\s*["\']([^"\']+)["\']',
            r'TOKEN\s*=\s*["\']([^"\']+)["\']',
            r'bot_token\s*=\s*["\']([^"\']+)["\']',
            r'token\s*=\s*["\']([^"\']+)["\']',
            r'["\']([0-9]{8,11}:[A-Za-z0-9_-]{35})["\']'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, python_code)
            if matches:
                token = matches[0]
                logger.info(f"✅ Bot token found using pattern: {pattern}")
                return token, True
        
        return None, False
    
    @staticmethod
    def validate_bot_token(token: str) -> bool:
        """Validate if the token format is correct"""
        # Telegram bot token format: 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
        pattern = r'^[0-9]{8,11}:[A-Za-z0-9_-]{35}$'
        return bool(re.match(pattern, token))
    
    @staticmethod
    async def get_bot_username(token: str) -> str:
        """Get bot username from Telegram API"""
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('ok'):
                            return data['result']['username']
                    else:
                        logger.error(f"❌ Failed to get bot info: {response.status}")
        except Exception as e:
            logger.error(f"❌ Error getting bot username: {e}")
        
        return "unknown"
    
    @staticmethod
    def create_bot_wrapper(python_code: str, bot_token: str, user_id: int, bot_id: int) -> str:
        """Create a wrapped bot code with proper token integration"""
        
        # Remove any existing token assignments
        cleaned_code = re.sub(
            r'(BOT_TOKEN|TOKEN|bot_token|token)\s*=\s*["\'][^"\']*["\']',
            f'BOT_TOKEN = "{bot_token}"',
            python_code
        )
        
        # If no token was replaced, add one
        if cleaned_code == python_code:
            # Find a good place to insert the token (after imports)
            import_section_end = 0
            lines = python_code.split('\n')
            for i, line in enumerate(lines):
                if line.strip() and not (line.startswith('import ') or line.startswith('from ')):
                    import_section_end = i
                    break
            
            if import_section_end == 0:
                import_section_end = len(lines)
            
            lines.insert(import_section_end, f'BOT_TOKEN = "{bot_token}"')
            cleaned_code = '\n'.join(lines)
        
        # Add health monitoring
        wrapper_code = f'''
# Auto-generated wrapper for bot hosting service
# Bot ID: {bot_id}
# User ID: {user_id}
# Created: {datetime.now()}

import os
import logging
import requests
import asyncio
from threading import Thread
import time

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Health monitoring
def start_health_monitor():
    """Monitor bot health and send periodic pings"""
    def monitor():
        while True:
            try:
                # Send heartbeat to main service
                requests.post(
                    '{get_app_url()}/bot_heartbeat',
                    json={{'bot_id': {bot_id}, 'status': 'active'}},
                    timeout=5
                )
            except:
                pass
            time.sleep(60)  # Check every minute
    
    Thread(target=monitor, daemon=True).start()

# Start health monitoring
start_health_monitor()

{cleaned_code}

logger.info("🤖 Bot started successfully with hosted service")
'''
        
        return wrapper_code

class BotDeployer:
    @staticmethod
    async def deploy_bot(user_id: int, python_code: str, requirements: str) -> dict:
        try:
            # Extract bot token from code
            bot_token, token_found = BotProcessor.extract_bot_token(python_code)
            
            if not token_found:
                return {
                    'success': False, 
                    'error': 'No bot token found in the code. Please include your bot token in the Python file.'
                }
            
            # Validate token format
            if not BotProcessor.validate_bot_token(bot_token):
                return {
                    'success': False,
                    'error': 'Invalid bot token format. Token should be in format: 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ'
                }
            
            # Get bot username
            bot_username = await BotProcessor.get_bot_username(bot_token)
            
            # Create user-specific directory
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            user_dir = f"user_bots/{user_id}_{timestamp}"
            os.makedirs(user_dir, exist_ok=True)
            
            # Basic security checks
            if BotDeployer._contains_malicious_code(python_code):
                return {'success': False, 'error': 'Code contains potentially malicious patterns'}
            
            # Create wrapped bot code with token integration
            bot_id = f"{user_id}_{timestamp}"
            wrapped_code = BotProcessor.create_bot_wrapper(python_code, bot_token, user_id, bot_id)
            
            # Save Python file
            bot_file = f"{user_dir}/bot.py"
            with open(bot_file, 'w', encoding='utf-8') as f:
                f.write(wrapped_code)
            
            # Save requirements
            req_file = f"{user_dir}/requirements.txt"
            with open(req_file, 'w', encoding='utf-8') as f:
                f.write(requirements)
            
            # Start the bot process
            deployment_result = await BotDeployer._start_bot_process(user_dir, bot_file)
            
            if deployment_result['success']:
                # Record deployment in database
                db_bot_id = DatabaseManager.add_bot_deployment(
                    user_id=user_id,
                    bot_token=bot_token,
                    bot_username=bot_username,
                    deployment_path=user_dir,
                    expires_at=datetime.now() + timedelta(days=30)
                )
                
                return {
                    'success': True,
                    'deployment_path': user_dir,
                    'process_id': deployment_result.get('process_id'),
                    'bot_token': bot_token,
                    'bot_username': bot_username,
                    'bot_id': db_bot_id
                }
            else:
                return deployment_result
            
        except Exception as e:
            logger.error(f"❌ Deployment error: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def _contains_malicious_code(code: str) -> bool:
        """Basic security check for malicious patterns"""
        dangerous_patterns = [
            "os.system", "subprocess.Popen", "eval(", "exec(", "__import__",
            "open(", "file(", "compile(", "input(", "reload(",
            "rm -rf", "del ", "format(", "pickle", "marshal",
            "import os", "import subprocess", "import sys",
            "__builtins__", "__globals__", "__code__"
        ]
        
        code_lower = code.lower()
        for pattern in dangerous_patterns:
            if pattern in code_lower:
                logger.warning(f"🚫 Blocked malicious pattern: {pattern}")
                return True
        return False
    
    @staticmethod
    async def _start_bot_process(user_dir: str, bot_file: str) -> dict:
        """Start bot process using subprocess"""
        try:
            # Install requirements first
            requirements_file = f"{user_dir}/requirements.txt"
            if os.path.exists(requirements_file):
                install_process = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'pip', 'install', '-r', requirements_file,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await install_process.communicate()
            
            # Start the bot
            process = await asyncio.create_subprocess_exec(
                sys.executable, bot_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=user_dir
            )
            
            # Wait a bit to see if it starts successfully
            await asyncio.sleep(5)
            
            if process.returncode is not None and process.returncode != 0:
                # Process failed
                stdout, stderr = await process.communicate()
                error_output = stderr.decode() if stderr else stdout.decode()
                return {
                    'success': False,
                    'error': f'Bot process failed to start: {error_output}'
                }
            
            return {
                'success': True,
                'process_id': process.pid,
                'process': process
            }
            
        except Exception as e:
            return {'success': False, 'error': str(e)}

# Add bot heartbeat endpoint
@app.route('/bot_heartbeat', methods=['POST'])
def bot_heartbeat():
    """Receive heartbeat from deployed bots"""
    try:
        data = request.get_json()
        bot_id = data.get('bot_id')
        status = data.get('status', 'active')
        
        if bot_id:
            DatabaseManager.update_bot_status(bot_id, status, is_active=True)
            return jsonify({'status': 'acknowledged'})
        
        return jsonify({'error': 'No bot_id provided'}), 400
    
    except Exception as e:
        logger.error(f"❌ Heartbeat error: {e}")
        return jsonify({'error': str(e)}), 500

class BotHostingService:
    def __init__(self, token: str):
        self.token = token
        self.application = Application.builder().token(token).build()
        self.keep_alive = KeepAliveSystem()
        self.active_processes = {}
        
        # Initialize database
        DatabaseManager.init_database()
        
        # Setup handlers
        self.setup_handlers()
        
    def setup_handlers(self):
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("deploy", self.deploy))
        self.application.add_handler(CommandHandler("mybots", self.my_bots))
        self.application.add_handler(CommandHandler("status", self.bot_status))
        self.application.add_handler(CommandHandler("balance", self.check_balance))
        self.application.add_handler(CommandHandler("ping", self.ping_service))
        self.application.add_handler(CommandHandler("stats", self.system_stats))
        self.application.add_handler(CommandHandler("upgrade", self.upgrade_tier))
        self.application.add_handler(CommandHandler("tier", self.check_tier))
        
        # Message handlers
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
    async def start(self, update: Update, context: CallbackContext):
        user = update.effective_user
        user_tier = DatabaseManager.get_user_tier(user.id)
        
        welcome_text = f"""
🤖 Welcome to Python Bot Hosting Service!

👤 Your Tier: {user_tier['tier'].upper()} {'👑' if user_tier['is_admin'] else '⭐'}
📊 Bot Limit: {user_tier['max_bots']} bot{'s' if user_tier['max_bots'] > 1 else ''}

With this service, you can:
• Deploy your Python Telegram bots 24/7
• Upload bot code + requirements.txt
• We automatically detect and integrate your bot token
• Monitor your bot status
• Manage multiple bots

💰 Pricing:
• Regular: 20 Stars ⭐ per bot (max 1 bot)
• Premium: 20 Stars ⭐ per bot (max 20 bots)
• Admins: FREE (unlimited)

Available commands:
/deploy - Start deploying a new bot
/mybots - List your deployed bots
/status <bot_id> - Check bot status
/balance - Check your star balance
/tier - Check your current tier
/upgrade - Upgrade to premium tier
/ping - Check service status
/stats - System statistics

📁 Just send your Python bot file and requirements.txt!
We'll automatically extract and integrate your bot token.
        """
        
        keyboard = [
            [InlineKeyboardButton("🚀 Deploy Bot", callback_data="deploy")],
            [InlineKeyboardButton("📊 My Bots", callback_data="mybots")],
            [InlineKeyboardButton("⭐ Check Balance", callback_data="balance")],
            [InlineKeyboardButton("👑 Check Tier", callback_data="tier")],
            [InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade")],
            [InlineKeyboardButton("📈 System Stats", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    
    async def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        user_tier = DatabaseManager.get_user_tier(user_id)
        return user_tier['is_admin']
    
    async def deploy(self, update: Update, context: CallbackContext):
        user = update.effective_user
        user_tier = DatabaseManager.get_user_tier(user.id)
        current_bots = DatabaseManager.get_user_bot_count(user.id)
        
        # Check bot limit
        if current_bots >= user_tier['max_bots'] and not user_tier['is_admin']:
            if user_tier['tier'] == 'regular':
                await update.message.reply_text(
                    f"❌ You've reached your bot limit!\n\n"
                    f"Regular users can only deploy 1 bot.\n"
                    f"Use /upgrade to get premium and deploy up to 20 bots!\n\n"
                    f"Your current bots: {current_bots}/{user_tier['max_bots']}"
                )
            else:
                await update.message.reply_text(
                    f"❌ You've reached your bot limit!\n\n"
                    f"Premium users can deploy up to 20 bots.\n"
                    f"Your current bots: {current_bots}/{user_tier['max_bots']}\n\n"
                    f"Please remove some bots before deploying new ones."
                )
            return
        
        is_admin = user_tier['is_admin']
        
        deploy_text = f"""
🚀 Bot Deployment Process:

👤 Your Tier: {user_tier['tier'].upper()}
📊 Bot Limit: {current_bots}/{user_tier['max_bots']} used

1. Send your main Python bot file (must contain bot token)
2. Send your requirements.txt file
3. We'll automatically deploy it!

🔍 We automatically:
• Extract your bot token from the code
• Validate the token with Telegram
• Create a secure wrapper
• Deploy your bot 24/7

💰 Cost: {'FREE' if is_admin else '20 Stars ⭐'} for 30 days

Send your Python file first...
        """
        
        # Store user state for file upload
        context.user_data['deploying'] = True
        context.user_data['files'] = {}
        
        await update.message.reply_text(deploy_text)
    
    async def handle_document(self, update: Update, context: CallbackContext):
        user = update.effective_user
        document = update.message.document
        
        if not context.user_data.get('deploying'):
            await update.message.reply_text("Please start with /deploy first!")
            return
        
        # Check file type
        file_name = document.file_name.lower()
        
        if file_name.endswith('.py'):
            # Download and read Python file
            file = await document.get_file()
            file_content = await self.download_file_content(file)
            
            context.user_data['files']['python'] = file_content
            await update.message.reply_text("✅ Python file received! Now send requirements.txt")
        
        elif file_name == 'requirements.txt':
            # Download and read requirements
            file = await document.get_file()
            file_content = await self.download_file_content(file)
            
            context.user_data['files']['requirements'] = file_content
            await update.message.reply_text("✅ requirements.txt received! Starting deployment...")
            
            # Start deployment process
            await self.process_deployment(update, context)
        
        else:
            await update.message.reply_text("❌ Please send only .py files or requirements.txt")
    
    async def download_file_content(self, file) -> str:
        """Download file content as text"""
        # Create temporary file path
        temp_path = f"temp_{int(time.time())}.txt"
        await file.download_to_drive(temp_path)
        
        # Read content
        with open(temp_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Clean up
        try:
            os.remove(temp_path)
        except:
            pass
        
        return content
    
    async def process_deployment(self, update: Update, context: CallbackContext):
        user = update.effective_user
        files = context.user_data.get('files', {})
        
        if 'python' not in files:
            await update.message.reply_text("❌ Need Python file to deploy")
            return
        
        user_tier = DatabaseManager.get_user_tier(user.id)
        is_admin = user_tier['is_admin']
        
        if not is_admin:
            # Check if user has enough stars
            user_balance = PaymentManager.get_user_balance(user.id)
            if user_balance < 20:
                await update.message.reply_text(
                    f"❌ Insufficient balance! You need 20 Stars ⭐ but have {user_balance}\n"
                    "Please add stars and try again."
                )
                return
        
        # Get requirements or use default
        requirements = files.get('requirements', 'python-telegram-bot==20.7\nrequests\n')
        
        await update.message.reply_text("🔍 Analyzing your bot code...")
        
        # Extract and validate token first
        bot_token, token_found = BotProcessor.extract_bot_token(files['python'])
        
        if not token_found:
            await update.message.reply_text(
                "❌ No bot token found in your code!\n\n"
                "Please include your bot token in one of these formats:\n"
                "• BOT_TOKEN = 'your_token_here'\n"
                "• TOKEN = 'your_token_here'\n"
                "• bot_token = 'your_token_here'\n"
                "• token = 'your_token_here'"
            )
            return
        
        if not BotProcessor.validate_bot_token(bot_token):
            await update.message.reply_text(
                "❌ Invalid bot token format!\n\n"
                "Your token should look like:\n"
                "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ\n\n"
                "Get your token from @BotFather"
            )
            return
        
        await update.message.reply_text("✅ Token validated! Starting deployment...")
        
        deployment_result = await BotDeployer.deploy_bot(
            user_id=user.id,
            python_code=files['python'],
            requirements=requirements
        )
        
        if deployment_result['success']:
            # Deduct payment if not admin
            if not is_admin:
                PaymentManager.deduct_stars(user.id, 20)
            
            bot_username = deployment_result.get('bot_username', 'unknown')
            bot_id = deployment_result.get('bot_id')
            current_bots = DatabaseManager.get_user_bot_count(user.id)
            
            await update.message.reply_text(
                f"✅ Bot deployed successfully!\n\n"
                f"🤖 Bot: @{bot_username}\n"
                f"🆔 Deployment ID: {bot_id}\n"
                f"👤 Your Tier: {user_tier['tier'].upper()}\n"
                f"📊 Bots Deployed: {current_bots}/{user_tier['max_bots']}\n"
                f"⏰ Expires: {(datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')}\n"
                f"📊 Check status with: /status {bot_id}"
            )
        else:
            await update.message.reply_text(f"❌ Deployment failed: {deployment_result['error']}")
        
        # Clean up user data
        context.user_data['deploying'] = False
        context.user_data['files'] = {}
    
    async def my_bots(self, update: Update, context: CallbackContext):
        user = update.effective_user
        bots = DatabaseManager.get_user_bots(user.id)
        user_tier = DatabaseManager.get_user_tier(user.id)
        
        if not bots:
            await update.message.reply_text("You have no deployed bots. Use /deploy to get started!")
            return
        
        bot_list = f"🤖 Your Deployed Bots ({len(bots)}/{user_tier['max_bots']}):\n\n"
        for bot in bots:
            status_icon = "🟢" if bot['is_active'] else "🔴"
            bot_list += f"{status_icon} @{bot['bot_username']} (ID: {bot['id']})\n"
            bot_list += f"   Status: {bot['status']}\n"
            bot_list += f"   Expires: {bot['expires_at'][:10] if bot['expires_at'] else 'N/A'}\n"
            bot_list += f"   Created: {bot['created_at'][:10]}\n\n"
        
        await update.message.reply_text(bot_list)
    
    async def bot_status(self, update: Update, context: CallbackContext):
        if not context.args:
            await update.message.reply_text("Please provide bot ID: /status <bot_id>")
            return
        
        bot_id = context.args[0]
        bot_info = DatabaseManager.get_bot_info(bot_id)
        
        if not bot_info:
            await update.message.reply_text("❌ Bot not found!")
            return
        
        if bot_info['user_id'] != update.effective_user.id and not await self.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Access denied!")
            return
        
        status_icon = "🟢" if bot_info['is_active'] else "🔴"
        status_text = f"""
🤖 Bot Status:

{status_icon} @{bot_info['bot_username']}
🆔 ID: {bot_info['id']}
📊 Status: {bot_info['status']}
⏰ Expires: {bot_info['expires_at'][:10] if bot_info['expires_at'] else 'N/A'}
📅 Created: {bot_info['created_at'][:10]}
🔄 Restarts: {bot_info['restart_count']}
📡 Last Ping: {bot_info['last_ping'][:16] if bot_info['last_ping'] else 'Never'}
        """
        
        await update.message.reply_text(status_text)
    
    async def check_balance(self, update: Update, context: CallbackContext):
        user = update.effective_user
        balance = PaymentManager.get_user_balance(user.id)
        user_tier = DatabaseManager.get_user_tier(user.id)
        current_bots = DatabaseManager.get_user_bot_count(user.id)
        
        balance_text = f"""
⭐ Your Balance:

Stars: {balance} ⭐
👤 Tier: {user_tier['tier'].upper()} {'👑' if user_tier['is_admin'] else ''}
📊 Bots: {current_bots}/{user_tier['max_bots']}

{'💫 You can deploy bots for FREE!' if user_tier['is_admin'] else f'💫 You can deploy {balance // 20} more bot(s)'}
{'🚀 Upgrade to premium for 20 bot slots!' if user_tier['tier'] == 'regular' and current_bots >= 1 else ''}
        """
        
        await update.message.reply_text(balance_text)
    
    async def check_tier(self, update: Update, context: CallbackContext):
        """Check user tier and limits"""
        user = update.effective_user
        user_tier = DatabaseManager.get_user_tier(user.id)
        current_bots = DatabaseManager.get_user_bot_count(user.id)
        
        tier_text = f"""
👤 Your Account Tier:

🎯 Tier: {user_tier['tier'].upper()} {'👑' if user_tier['is_admin'] else '⭐'}
📊 Bot Limit: {user_tier['max_bots']} bot{'s' if user_tier['max_bots'] > 1 else ''}
🤖 Current Bots: {current_bots}/{user_tier['max_bots']}

{'💎 Premium Features:' if user_tier['tier'] == 'premium' else '🎁 Regular Features:'}
• {'✅' if user_tier['tier'] == 'premium' else '❌'} Up to {user_tier['max_bots']} simultaneous bots
• {'✅' if user_tier['tier'] == 'premium' else '❌'} Priority deployment
• {'✅' if user_tier['tier'] == 'premium' else '❌'} Extended bot lifetime

{'🚀 Use /upgrade to get premium features!' if user_tier['tier'] == 'regular' else '🎉 You are enjoying premium features!'}
        """
        
        await update.message.reply_text(tier_text)
    
    async def upgrade_tier(self, update: Update, context: CallbackContext):
        """Upgrade user to premium tier"""
        user = update.effective_user
        user_tier = DatabaseManager.get_user_tier(user.id)
        
        if user_tier['tier'] == 'premium':
            await update.message.reply_text(
                "🎉 You are already a premium user!\n\n"
                "You can deploy up to 20 bots simultaneously.\n"
                "Enjoy all the premium features! 🚀"
            )
            return
        
        upgrade_cost = 100  # Stars to upgrade to premium
        
        user_balance = PaymentManager.get_user_balance(user.id)
        
        if user_balance < upgrade_cost:
            await update.message.reply_text(
                f"❌ Insufficient balance for upgrade!\n\n"
                f"Upgrade cost: {upgrade_cost} Stars ⭐\n"
                f"Your balance: {user_balance} Stars ⭐\n\n"
                f"You need {upgrade_cost - user_balance} more stars to upgrade to premium."
            )
            return
        
        # Process upgrade
        if PaymentManager.deduct_stars(user.id, upgrade_cost):
            DatabaseManager.upgrade_user_tier(user.id, 'premium')
            
            await update.message.reply_text(
                f"🎉 Congratulations! You've been upgraded to PREMIUM tier!\n\n"
                f"✨ New Features Unlocked:\n"
                f"• Deploy up to 20 bots simultaneously\n"
                f"• Priority deployment queue\n"
                f"• Extended bot lifetime\n"
                f"• Premium support\n\n"
                f"💰 Cost: {upgrade_cost} Stars ⭐\n"
                f"💎 Your new tier: PREMIUM\n"
                f"🚀 Start deploying more bots with /deploy"
            )
        else:
            await update.message.reply_text("❌ Upgrade failed. Please try again.")
    
    async def ping_service(self, update: Update, context: CallbackContext):
        """Check service status"""
        app_url = get_app_url()
        status_text = f"""
🏓 Service Status:

✅ Bot Hosting Service: Active
⏰ Uptime: {get_uptime()}
🤖 Active Bots: {DatabaseManager.get_active_bots_count()}
👥 Total Users: {DatabaseManager.get_total_users()}
📊 Total Deployments: {DatabaseManager.get_total_deployments()}
🔋 Keep-alive: Running (4-minute intervals)

🌐 App URL: {app_url}
📡 Health: {app_url}/health
        """
        
        await update.message.reply_text(status_text)
    
    async def system_stats(self, update: Update, context: CallbackContext):
        """Show system statistics"""
        app_url = get_app_url()
        stats_text = f"""
📈 System Statistics:

🤖 Active Bots: {DatabaseManager.get_active_bots_count()}
👥 Total Users: {DatabaseManager.get_total_users()}
🚀 Total Deployments: {DatabaseManager.get_total_deployments()}
⏰ Service Uptime: {get_uptime()}
🔋 Keep-alive: 4-minute intervals

💾 Database: Connected
🌐 Web Server: Running
🔄 Background Tasks: Active
🔗 App URL: {app_url}
        """
        
        await update.message.reply_text(stats_text)
    
    async def button_handler(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()
        
        if query.data == "deploy":
            await self.deploy(update, context)
        elif query.data == "mybots":
            await self.my_bots(update, context)
        elif query.data == "balance":
            await self.check_balance(update, context)
        elif query.data == "tier":
            await self.check_tier(update, context)
        elif query.data == "upgrade":
            await self.upgrade_tier(update, context)
        elif query.data == "stats":
            await self.system_stats(update, context)
    
    async def start_keep_alive(self):
        """Start keep-alive system"""
        asyncio.create_task(self.keep_alive.start())
    
    def run(self):
        """Start the bot"""
        logger.info("🚀 Starting Bot Hosting Service...")
        
        # Start health check server
        start_health_server()
        
        # Start keep-alive system
        asyncio.get_event_loop().run_until_complete(self.start_keep_alive())
        
        # Start bot
        logger.info("🤖 Telegram Bot is now running...")
        self.application.run_polling()

def main():
    # Get bot token from environment
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        logger.error("❌ Please set BOT_TOKEN environment variable")
        return
    
    # Create necessary directories
    os.makedirs("user_bots", exist_ok=True)
    
    # Start the service
    hosting_service = BotHostingService(BOT_TOKEN)
    hosting_service.run()

if __name__ == "__main__":
    main()
