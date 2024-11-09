import telebot
from telebot import types
import re
import sqlite3
from datetime import datetime
from functools import lru_cache
from contextlib import contextmanager
from typing import Optional, Tuple, List, Dict
from math import log2, floor, isnan, isinf
import threading
# Thay thế 'YOUR_BOT_TOKEN' bằng token thực của bot của bạn
bot = telebot.TeleBot('7170561406:AAHcdJ-h_lUtNfGQokWBhei3KT1JxklgUTY')

# Lưu trữ trạng thái
user_state = {}

class DatabaseManager:
    def __init__(self, db_name: str = 'bot_database.db'):
        """
        Khởi tạo DatabaseManager với connection pooling và thread safety.
        
        Args:
            db_name: Tên file database
        """
        self.db_name = db_name
        self._local = threading.local()
        self._lock = threading.Lock()
        self.initialize_db()

    @contextmanager
    def get_connection(self):
        """
        Context manager để quản lý connection pool.
        Tự động đóng connection sau khi sử dụng.
        """
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(self.db_name, timeout=20)
            # Enable WAL mode for better concurrent access
            self._local.connection.execute('PRAGMA journal_mode=WAL')
            # Enable foreign key constraints
            self._local.connection.execute('PRAGMA foreign_keys=ON')
        
        try:
            yield self._local.connection
        finally:
            if hasattr(self._local, 'connection'):
                self._local.connection.close()
                del self._local.connection

    def initialize_db(self) -> None:
        """Khởi tạo database với indexes để tối ưu truy vấn."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Tạo bảng users với các indexes phù hợp
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id_tele INTEGER PRIMARY KEY,
                hoten TEXT NOT NULL,
                username TEXT,
                last_time_using TEXT NOT NULL,
                convert_all INTEGER DEFAULT 0
            )
            ''')
            
            # Tạo index cho username để tìm kiếm nhanh
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_username ON users(username)')
            
            # Tạo bảng conversion_history với cấu trúc tối ưu
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversion_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_tele INTEGER NOT NULL,
                conversion_text TEXT NOT NULL,
                conversion_time TEXT NOT NULL,
                FOREIGN KEY (id_tele) REFERENCES users (id_tele) ON DELETE CASCADE
            )
            ''')
            
            # Tạo index cho id_tele và conversion_time để tối ưu truy vấn lịch sử
            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversion_history 
            ON conversion_history(id_tele, conversion_time DESC)
            ''')
            
            conn.commit()

    def update_user_data(self, user) -> None:
        """
        Cập nhật thông tin người dùng với prepared statement.
        
        Args:
            user: Đối tượng user từ Telegram
        """
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        
        with self.get_connection() as conn:
            conn.execute('''
            INSERT INTO users (id_tele, hoten, username, last_time_using)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id_tele) DO UPDATE SET
                hoten = excluded.hoten,
                username = excluded.username,
                last_time_using = excluded.last_time_using
            ''', (user.id, full_name, user.username, current_time))
            conn.commit()

    def update_convert_all(self, user_id: int) -> None:
        """
        Tăng số lần chuyển đổi với prepared statement.
        
        Args:
            user_id: ID của người dùng
        """
        with self.get_connection() as conn:
            conn.execute('''
            UPDATE users 
            SET convert_all = convert_all + 1,
                last_time_using = ?
            WHERE id_tele = ?
            ''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
            conn.commit()

    def add_conversion_history(self, user_id: int, conversion_text: str) -> None:
        """
        Thêm lịch sử chuyển đổi với prepared statement.
        
        Args:
            user_id: ID của người dùng
            conversion_text: Nội dung chuyển đổi
        """
        with self.get_connection() as conn:
            conn.execute('''
            INSERT INTO conversion_history (id_tele, conversion_text, conversion_time)
            VALUES (?, ?, ?)
            ''', (user_id, conversion_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()

    def get_user_history(self, user_id: int, limit: int = 10) -> Tuple[int, List[str]]:
        """
        Lấy lịch sử chuyển đổi với prepared statement và tối ưu query.
        
        Args:
            user_id: ID của người dùng
            limit: Số lượng lịch sử muốn lấy
            
        Returns:
            Tuple chứa tổng số lần chuyển đổi và danh sách lịch sử
        """
        with self.get_connection() as conn:
            # Sử dụng một transaction cho nhiều queries
            cursor = conn.cursor()
            
            # Lấy tổng số lần chuyển đổi
            cursor.execute(
                'SELECT convert_all FROM users WHERE id_tele = ?', 
                (user_id,)
            )
            total = cursor.fetchone()
            total_conversions = total[0] if total else 0
            
            # Lấy lịch sử gần nhất với index optimization
            cursor.execute('''
            SELECT conversion_text 
            FROM conversion_history 
            WHERE id_tele = ? 
            ORDER BY conversion_time DESC 
            LIMIT ?
            ''', (user_id, limit))
            
            history = cursor.fetchall()
            return total_conversions, [row[0] for row in history]

    def clear_user_history(self, user_id: int) -> None:
        """
        Xóa lịch sử chuyển đổi trong một transaction.
        
        Args:
            user_id: ID của người dùng
        """
        with self.get_connection() as conn:
            # Thực hiện cả hai operations trong cùng một transaction
            conn.execute('BEGIN')
            try:
                conn.execute(
                    'DELETE FROM conversion_history WHERE id_tele = ?', 
                    (user_id,)
                )
                conn.execute(
                    'UPDATE users SET convert_all = 0 WHERE id_tele = ?', 
                    (user_id,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

db = DatabaseManager()

def detect_base(num_str):
# Kiểm tra hệ nhị phân (hệ 2)
    if re.match(r'^[01]+$', num_str):
        return 2
# Kiểm tra hệ bát phân (hệ 8)
    elif re.match(r'^[0-7]+$', num_str):
        return 8
# Kiểm tra hệ thập phân (hệ 10)
    elif re.match(r'^[0-9]+$', num_str):
        return 10
# Kiểm tra hệ thập lục phân (hệ 16)
    elif re.match(r'^[0-9A-Fa-f]+$', num_str):
        return 16
    else:
        raise ValueError("Không thể xác định hệ cơ số. Vui lòng nhập một số hợp lệ.")
# Tạo lookup table để tối ưu việc chuyển đổi
BINARY_LOOKUP: Dict[int, str] = {i: format(i, 'b') for i in range(256)}
COMPLEMENT_TABLE = str.maketrans('01', '10')

@lru_cache(maxsize=1024)
def _get_binary_str(num: int, bits: int) -> str:
    """Helper function để cache các kết quả chuyển đổi phổ biến."""
    if 0 <= num < 256:
        return BINARY_LOOKUP[num].zfill(bits)
    return format(num, f'0{bits}b')

@lru_cache(maxsize=1024)
def convert_to_signed_binary(num_str: str, bits: int = 8) -> Tuple[str, str]:
    """
    Chuyển đổi số thập phân sang số nhị phân có dấu (phiên bản tối ưu).
    """
    try:
        num = int(num_str)
    except ValueError:
        raise ValueError(f"'{num_str}' không phải là số nguyên hợp lệ")

    is_negative = num < 0
    abs_num = abs(num)
    
    # Kiểm tra giới hạn của số
    max_value = (1 << (bits - 1)) - 1
    min_value = -(1 << (bits - 1))
    if not min_value <= num <= max_value:
        raise ValueError(f"Số nằm ngoài phạm vi [{min_value}, {max_value}]")

    explanation: List[str] = [f"Chuyển đổi {num_str} sang nhị phân có dấu:"]
    
    if is_negative:
        explanation.append(f"1. Bỏ dấu trừ: {abs_num}")
        
        # Sử dụng helper function đã được cache
        binary = _get_binary_str(abs_num, bits)
        explanation.append(f"2. Chuyển sang nhị phân {bits}-bit: {binary}")
        
        # Tối ưu việc lấy bù 1 với translation table
        complement_one = binary.translate(COMPLEMENT_TABLE)
        explanation.append(f"3. Lấy bù 1 (đảo bit): {complement_one}")
        
        # Tối ưu việc lấy bù 2 với bitwise operations
        complement_two = _get_binary_str((int(complement_one, 2) + 1) & ((1 << bits) - 1), bits)
        explanation.append(f"4. Cộng thêm 1 để có bù 2: {complement_two}")
        
        return complement_two, '\n'.join(explanation)
    
    binary = _get_binary_str(abs_num, bits)
    explanation.extend([
        f"1. Chuyển sang nhị phân {bits}-bit: {binary}",
        "Số dương nên không cần chuyển đổi thêm."
    ])
    
    return binary, '\n'.join(explanation)

@lru_cache(maxsize=1024)
def convert_float_to_binary(num_str: str, precision: int = 10) -> Tuple[str, str]:
    """
    Chuyển đổi số thực sang dạng nhị phân (phiên bản tối ưu).
    """
    # Validation
    try:
        num = float(num_str)
    except ValueError:
        raise ValueError(f"'{num_str}' không phải là số hợp lệ")
    
    if precision < 0:
        raise ValueError("Độ chính xác không được là số âm")
    
    # Fast path cho các trường hợp đặc biệt
    if num == 0:
        return "0", "Số 0 trong hệ nhị phân là 0"
    elif isnan(num):
        return "NaN", "Không phải là số (NaN)"
    elif isinf(num):
        result = "-inf" if num < 0 else "inf"
        return result, f"Số vô cùng ({result})"
    
    # Xử lý dấu
    sign = "-" if num < 0 else "+"
    num = abs(num)
    
    # Tối ưu việc tách phần nguyên và thập phân
    int_part = int(num)
    decimal_part = num - int_part
    
    # Chuyển đổi phần nguyên sử dụng helper function
    int_binary = _get_binary_str(int_part, max(1, int_part.bit_length()))
    
    # Tối ưu việc chuyển đổi phần thập phân
    binary_decimal = []
    decimal_steps = []
    current = decimal_part
    
    # Sử dụng loop được tối ưu
    for _ in range(precision):
        current *= 2
        bit = int(current)
        binary_decimal.append(str(bit))
        
        decimal_steps.append(
            f"   * {decimal_part:.6f} × 2 = {current:.6f} → {bit}"
        )
        
        if bit == 1:
            current -= 1
        
        decimal_part = current
        if decimal_part == 0:
            break
    
    # Tạo kết quả
    result = [sign, int_binary]
    if binary_decimal:
        result.extend([".", "".join(binary_decimal)])
    
    # Tạo giải thích
    explanation = [
        f"Chuyển đổi số thực {num_str} sang nhị phân:",
        f"1. Xác định dấu: {sign}",
        f"2. Chuyển đổi phần nguyên {int_part}:",
        f"   {int_part} (10) = {int_binary} (2)"
    ]
    
    if binary_decimal:
        explanation.extend([
            f"3. Chuyển đổi phần thập phân {num - int(num):.6f}:",
            *decimal_steps
        ])
    else:
        explanation.append("3. Không có phần thập phân")
    
    final_result = "".join(result)
    explanation.append(f"Kết quả cuối cùng: {final_result}")
    
    return final_result, "\n".join(explanation)
        
# Constants
HEX_DIGITS = "0123456789ABCDEF"
HEX_TO_DEC: Dict[str, int] = {c: i for i, c in enumerate(HEX_DIGITS)}
BINARY_TO_OCT = {format(i, '03b'): str(i) for i in range(8)}
BINARY_TO_HEX = {format(i, '04b'): HEX_DIGITS[i] for i in range(16)}
OCT_TO_BINARY = {str(i): format(i, '03b') for i in range(8)}
HEX_TO_BINARY = {HEX_DIGITS[i]: format(i, '04b') for i in range(16)}

@lru_cache(maxsize=5000)
def convert_base(num_str: str, from_base: int, to_base: int) -> Tuple[str, str]:
    """
    Chuyển đổi số từ hệ cơ số này sang hệ cơ số khác với giải thích chi tiết.
    
    Args:
        num_str: Số cần chuyển đổi dưới dạng chuỗi
        from_base: Hệ cơ số gốc (2, 8, 10, 16)
        to_base: Hệ cơ số đích (2, 8, 10, 16)
    
    Returns:
        Tuple gồm kết quả chuyển đổi và giải thích
    """
    if from_base == to_base:
        return num_str, "Không cần chuyển đổi vì cùng hệ cơ số."
        
    num_str = num_str.upper()
    explanation = f"Chuyển đổi {num_str} từ cơ số {from_base} sang cơ số {to_base}:\n\n"

    # Chuyển đổi sang hệ 10
    if to_base == 10:
        result = 0
        power = 1
        base_name = "8" if from_base == 8 else "16" if from_base == 16 else "2"
        explanation += f"Sử dụng phương pháp nhân với lũy thừa của {base_name}:\n"
        
        for i, digit in enumerate(reversed(num_str)):
            if from_base == 16:
                digit_value = HEX_TO_DEC[digit]
            else:
                digit_value = int(digit)
            
            contribution = digit_value * power
            result += contribution
            explanation += f"  {digit} * {base_name}^{i} = {digit_value} * {power} = {contribution}\n"
            power *= from_base
        
        explanation += f"Tổng: {result}\n"
        return str(result), explanation

    # Chuyển từ hệ 10
    if from_base == 10:
        decimal = int(num_str)
        if decimal == 0:
            return "0", explanation + "Số 0 giống nhau ở mọi hệ cơ số."
            
        # Tìm lũy thừa lớn nhất
        max_power = 0
        temp = decimal
        while temp >= to_base:
            temp //= to_base
            max_power += 1

        explanation += f"1. Tìm lũy thừa lớn nhất của {to_base} không vượt quá {decimal}: {to_base}^{max_power} = {to_base**max_power}\n\n"
        explanation += f"2. Xây dựng số từ trái sang phải:\n"

        result = []
        remaining = decimal

        for power in range(max_power, -1, -1):
            value = to_base ** power
            quotient = remaining // value
            digit = HEX_DIGITS[quotient] if to_base == 16 else str(quotient)
            result.append(digit)
            remaining -= quotient * value
            explanation += f"  - {remaining + quotient * value} ÷ {to_base}^{power} = {quotient}"
            if to_base == 16:
                explanation += f" ({digit})"
            explanation += f" (dư {remaining})\n"

        return ''.join(result), explanation

    # Chuyển đổi trực tiếp giữa hệ 2, 8, 16
    if from_base == 2:
        if to_base == 8:
            padding = '0' * ((3 - len(num_str) % 3) % 3)
            padded = padding + num_str
            groups = [padded[i:i+3] for i in range(0, len(padded), 3)]
            
            explanation += "Nhóm các bit thành nhóm 3 bit:\n"
            result = []
            
            for group in groups:
                oct_digit = BINARY_TO_OCT[group]
                result.append(oct_digit)
                explanation += f"  {group} (2) = {oct_digit} (8)\n"
            
            final_result = ''.join(result).lstrip('0') or '0'
            explanation += f"Kết quả cuối cùng: {final_result}\n"
            return final_result, explanation
            
        if to_base == 16:
            padding = '0' * ((4 - len(num_str) % 4) % 4)
            padded = padding + num_str
            groups = [padded[i:i+4] for i in range(0, len(padded), 4)]
            
            explanation += "Nhóm các bit thành nhóm 4 bit:\n"
            result = []
            
            for group in groups:
                hex_digit = BINARY_TO_HEX[group]
                result.append(hex_digit)
                explanation += f"  {group} (2) = {hex_digit} (16)\n"
            
            final_result = ''.join(result).lstrip('0') or '0'
            explanation += f"Kết quả cuối cùng: {final_result}\n"
            return final_result, explanation

    if from_base in [8, 16] and to_base == 2:
        explanation += f"Chuyển đổi từng chữ số sang nhị phân:\n"
        result = []
        
        for digit in num_str:
            if from_base == 8:
                binary = OCT_TO_BINARY[digit]
            else:
                binary = HEX_TO_BINARY[digit]
            result.append(binary)
            explanation += f"  {digit} ({from_base}) = {binary} (2)\n"
        
        final_result = ''.join(result).lstrip('0') or '0'
        explanation += f"Ghép các nhóm bit lại: {final_result}\n"
        return final_result, explanation

    # Chuyển đổi gián tiếp qua hệ nhị phân
    binary, first_exp = convert_base(num_str, from_base, 2)
    result, second_exp = convert_base(binary, 2, to_base)
    explanation = first_exp + "\nSau đó:\n" + second_exp
    return result, explanation
    
def convert_to_all_bases(num_str, from_base):
    # Hàm mới: chuyển đổi số đã nhập sang tất cả các hệ 2, 8, 10, 16
    conversions = {}
    for to_base in [2, 8, 10, 16]:
        if to_base != from_base:  # Bỏ qua chuyển đổi cùng hệ
            result, _ = (num_str, from_base, to_base)
            conversions[to_base] = result
    return conversions


def handle_user_input(message):
    chat_id = message.chat.id
    num_str = message.text.strip().upper()  # Chuyển về chữ hoa để xử lý hệ 16
    
    # Kiểm tra xem có phải là chuỗi nhị phân IEEE 754 không
    is_ieee, bits = is_ieee754_binary(num_str)
    if is_ieee:
        try:
            result, explanation = ieee754_to_decimal(num_str)
            response = explanation
            
            if len(response) > 4096:
                for x in range(0, len(response), 4096):
                    bot.send_message(chat_id, response[x:x+4096])
            else:
                bot.reply_to(message, response)
            
            conversion_history = f"{num_str} (IEEE 754) -> {result}"
            db.update_convert_all(chat_id)
            db.add_conversion_history(chat_id, conversion_history)
            
            user_state[chat_id] = {'step': 'input_number'}
            return
        except Exception as e:
            bot.reply_to(message, f"Lỗi: {str(e)}")
            return

    # Kiểm tra số thực
    try:
        float(num_str)
        if '.' in num_str:
            markup = types.ReplyKeyboardMarkup(row_width=2)
            markup.add(
                'Chuyển sang nhị phân đơn giản',
                'Chuyển sang IEEE 754 (32-bit)',
                'Chuyển sang IEEE 754 (64-bit)'
            )
            user_state[chat_id] = {'step': 'choose_float_conversion', 'number': num_str}
            bot.reply_to(message, 
                        "Hãy chọn cách chuyển đổi số thực:",
                        reply_markup=markup)
            return
    except ValueError:
        # Kiểm tra xem có phải là số hex hợp lệ không
        if all(c in '0123456789ABCDEFabcdef' for c in num_str):
            user_state[chat_id] = {'step': 'choose_input_base', 'number': num_str}
            markup = types.ReplyKeyboardMarkup(row_width=2)
            markup.add('16')  # Chỉ cho phép chọn hệ 16 vì đã xác định là số hex
            bot.reply_to(message, 
                        f"Số hex cần chuyển đổi là: {num_str}\n"
                        f"Xác nhận đây là số hệ 16:", 
                        reply_markup=markup)
            return
    
    # Kiểm tra số âm
    is_negative = num_str.startswith('-')
    if is_negative:
        remaining_num = num_str[1:]
        if not remaining_num.isdigit():
            bot.reply_to(message, "Vui lòng nhập một số nguyên âm hợp lệ.")
            return
        
        user_state[chat_id] = {'step': 'choose_bit_length', 'number': num_str}
        markup = types.ReplyKeyboardMarkup(row_width=2)
        markup.add('8 bit', '16 bit', '32 bit', '64 bit')
        bot.reply_to(message, 
                    f"Bạn muốn chuyển số {num_str} sang dạng nhị phân có dấu với bao nhiêu bit?",
                    reply_markup=markup)
        return
    
    # Xử lý số thông thường
    user_state[chat_id] = {'step': 'choose_input_base', 'number': num_str}
    markup = types.ReplyKeyboardMarkup(row_width=2)
    markup.add('Tự động nhận diện', '2', '8', '10', '16')
    bot.reply_to(message, 
                f"Số cần chuyển đổi là: {num_str}\n"
                f"Hãy chọn hệ cơ số đầu vào hoặc để bot tự động nhận diện:", 
                reply_markup=markup)
    
    # Cập nhật thông tin người dùng
    db.update_user_data(message.from_user)
    
    
def handle_bit_length_selection(message):
    chat_id = message.chat.id
    num_str = user_state[chat_id]['number']
    
    try:
        # Lấy số bit từ input (ví dụ: "8 bit" -> 8)
        bit_length = int(message.text.split()[0])
        
        if bit_length not in [8, 16, 32, 64]:
            raise ValueError("Độ dài bit không hợp lệ")
        
        # Thực hiện chuyển đổi với số bit đã chọn
        result, explanation = convert_to_signed_binary(num_str, bit_length)
        
        response = f"Chuyển đổi số âm {num_str} sang dạng nhị phân có dấu {bit_length} bit:\n\n{explanation}\n\nKết quả: {result}"
        
        # Chia nhỏ tin nhắn dài nếu cần
        if len(response) > 4096:
            for x in range(0, len(response), 4096):
                bot.send_message(chat_id, response[x:x+4096])
        else:
            bot.reply_to(message, response, reply_markup=types.ReplyKeyboardRemove())
        
        # Cập nhật số lần chuyển đổi và lịch sử chuyển đổi
        conversion_history = f"{num_str} (base 10) -> {result} ({bit_length}-bit signed binary)"
        db.update_convert_all(chat_id)
        db.add_conversion_history(chat_id, conversion_history)
        
        # Đặt lại trạng thái và yêu cầu chuyển đổi tiếp theo
        user_state[chat_id] = {'step': 'input_number'}
        bot.send_message(chat_id, "Bạn có thể bắt đầu một phép chuyển đổi mới bằng cách nhập một số khác.")
        
    except ValueError as e:
        bot.reply_to(message, f"Lỗi: {str(e)}. Vui lòng chọn một độ dài bit hợp lệ.")
    except Exception as e:
        bot.reply_to(message, f"Có lỗi xảy ra: {str(e)}. Vui lòng thử lại.")
        user_state[chat_id] = {'step': 'input_number'}


def handle_input_base_selection(message):
    chat_id = message.chat.id
    choice = message.text
    num_str = user_state[chat_id]['number'].upper()  # Chuyển về chữ hoa để xử lý hệ 16

    if choice == 'Tự động nhận diện':
        try:
            from_base = detect_base(num_str)
            user_state[chat_id]['from_base'] = from_base
            bot.reply_to(message, f"Hệ cơ số đầu vào được xác định là: {from_base}")
        except ValueError as e:
            bot.reply_to(message, str(e))
            return
    else:
        try:
            from_base = int(choice)
            if from_base not in [2, 8, 10, 16]:
                raise ValueError("Hệ cơ số không hợp lệ")
            
            # Kiểm tra tính hợp lệ của số trong hệ cơ số đã chọn
            if from_base == 16:
                # Kiểm tra đặc biệt cho hệ 16
                if not all(c in '0123456789ABCDEF' for c in num_str):
                    raise ValueError("Số không hợp lệ trong hệ cơ số 16")
            else:
                # Kiểm tra cho các hệ cơ số khác
                int(num_str, from_base)
            
            user_state[chat_id]['from_base'] = from_base
        except ValueError:
            bot.reply_to(message, "Hệ cơ số không hợp lệ hoặc số không phù hợp với hệ cơ số đã chọn. Vui lòng thử lại.")
            return

    user_state[chat_id]['step'] = 'choose_conversion'
    markup = types.ReplyKeyboardMarkup(row_width=2)
    markup.add('Chuyển đổi sang hệ khác', 'Chuyển đổi sang tất cả các hệ')
    bot.reply_to(message, "Hãy chọn một lựa chọn:", reply_markup=markup)

def handle_conversion_choice(message):
    chat_id = message.chat.id
    choice = message.text
    if choice == 'Chuyển đổi sang hệ khác':
        markup = types.ReplyKeyboardMarkup(row_width=2)
        markup.add('2', '8', '10', '16')
        bot.reply_to(message, "Hãy chọn cơ số đích:", reply_markup=markup)
        user_state[chat_id]['step'] = 'input_to_base'
    elif choice == 'Chuyển đổi sang tất cả các hệ':
        num = user_state[chat_id]['number']
        from_base = user_state[chat_id]['from_base']
        
        result_message = f"Kết quả chuyển đổi từ hệ {from_base}:\n"
        for to_base in [2, 8, 10, 16]:
            if to_base != from_base:
                result, _ = convert_base(num, from_base, to_base)
                result_message += f"- Hệ {to_base}: {result}\n"
        
        bot.reply_to(message, result_message, reply_markup=types.ReplyKeyboardRemove())
        
        conversion_history = f"{num} (base {from_base}) -> Tất cả các hệ"
        
        # Cập nhật thông tin chuyển đổi và lịch sử
        db.update_convert_all(chat_id)
        db.add_conversion_history(chat_id, conversion_history)

        user_state[chat_id] = {'step': 'input_number'}
        bot.send_message(chat_id, "Bạn có thể bắt đầu một phép chuyển đổi mới bằng cách nhập một số khác.")
    else:
        bot.reply_to(message, "Lựa chọn không hợp lệ. Vui lòng chọn lại.")


def handle_base_selection(message):
    chat_id = message.chat.id
    try:
        to_base = int(message.text)
        if to_base not in [2, 8, 10, 16]:
            raise ValueError("Hệ cơ số đích không hợp lệ. Vui lòng chọn 2, 8, 10 hoặc 16.")
        
        num = user_state[chat_id]['number']
        from_base = user_state[chat_id]['from_base']
        
        result, explanation = convert_base(num, from_base, to_base)
        
        response = f"Kết quả: {result}\n\nGiải thích:\n{explanation}"
        
        # Chia nhỏ tin nhắn dài nếu cần
        if len(response) > 4096:
            for x in range(0, len(response), 4096):
                bot.send_message(chat_id, response[x:x+4096])
        else:
            bot.reply_to(message, response, reply_markup=types.ReplyKeyboardRemove())
        
        conversion_history = f"{num} (base {from_base}) -> {result} (base {to_base})"
        
        # Cập nhật số lần chuyển đổi và lịch sử chuyển đổi
        db.update_convert_all(chat_id)
        db.add_conversion_history(chat_id, conversion_history)
        
        # Đặt lại trạng thái và yêu cầu chuyển đổi tiếp theo
        user_state[chat_id] = {'step': 'input_number'}
        bot.send_message(chat_id, "Bạn có thể bắt đầu một phép chuyển đổi mới bằng cách nhập một số khác.")
    except ValueError as e:
        bot.reply_to(message, f"Lỗi: {str(e)}. Vui lòng thử lại.")
    except Exception as e:
        bot.reply_to(message, f"Có lỗi xảy ra: {str(e)}. Vui lòng thử lại.")
        user_state[chat_id] = {'step': 'input_number'}

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, 
        "Chào mừng! Bot có thể:\n"
        "1. Chuyển đổi giữa các hệ cơ số 2, 8, 10, 16\n"
        "2. Chuyển đổi số âm sang nhị phân có dấu\n"
        "3. Chuyển đổi số thực sang nhị phân đơn giản hoặc IEEE 754\n"
        "4. Chuyển đổi từ IEEE 754 sang số thực\n\n"
        "Các lệnh có sẵn:\n"
        "/history - Xem lịch sử chuyển đổi\n"
        "/clear_history - Xóa lịch sử chuyển đổi\n\n"
        "Hãy nhập số cần chuyển đổi để bắt đầu!")
    user_state[message.chat.id] = {'step': 'input_number'}
    db.update_user_data(message.from_user)

@bot.message_handler(commands=['history'])
def show_history(message):
    chat_id = message.chat.id
    try:
        total_conversions, history_list = db.get_user_history(chat_id)
        
        if history_list:
            response = f"Tổng số lần chuyển đổi: {total_conversions}\n\n"
            response += "10 lần chuyển đổi gần nhất:\n\n"
            response += "\n".join(history_list)
            
            if len(response) > 4096:
                for x in range(0, len(response), 4096):
                    bot.send_message(chat_id, response[x:x+4096])
            else:
                bot.reply_to(message, response)
        else:
            bot.reply_to(message, "Bạn chưa có lịch sử chuyển đổi nào.")
    except Exception as e:
        bot.reply_to(message, f"Có lỗi xảy ra khi đọc lịch sử: {str(e)}")


@bot.message_handler(commands=['clear_history'])
def clear_history(message):
    chat_id = message.chat.id
    try:
        db.clear_user_history(chat_id)
        bot.reply_to(message, "Lịch sử chuyển đổi đã được xóa.")
    except Exception as e:
        bot.reply_to(message, f"Có lỗi xảy ra khi xóa lịch sử: {str(e)}")

@bot.message_handler(func=lambda message: True)
def handle_conversion(message):
    chat_id = message.chat.id
    current_step = user_state.get(chat_id, {}).get('step', 'input_number')

    if current_step == 'input_number':
        handle_user_input(message)
    elif current_step == 'choose_input_base':
        handle_input_base_selection(message)
    elif current_step == 'choose_conversion':
        handle_conversion_choice(message)
    elif current_step == 'input_to_base':
        handle_base_selection(message)
    elif current_step == 'choose_bit_length':
        handle_bit_length_selection(message)
    elif current_step == 'choose_float_conversion':  # Thêm case mới
        handle_float_conversion_choice(message)

    # Cập nhật thời gian sử dụng cuối cùng
    db.update_user_data(message.from_user)
  
def is_ieee754_binary(binary_str: str) -> tuple[bool, int]:
    """
    Kiểm tra xem một chuỗi nhị phân có phải là số IEEE 754 hay không.
    Trả về (True, bits) nếu là IEEE 754, với bits là 32 hoặc 64.
    """
    if not all(bit in '01' for bit in binary_str):
        return False, 0
        
    if len(binary_str) == 32:
        return True, 32
    elif len(binary_str) == 64:
        return True, 64
    return False, 0
    
@lru_cache(maxsize=1024)
def _get_ieee_params(bits: int) -> Tuple[int, int, int]:
    """Cache các thông số IEEE 754 để tránh tính toán lặp lại."""
    if bits == 32:
        return 8, 23, 127
    elif bits == 64:
        return 11, 52, 1023
    raise ValueError("Số bit phải là 32 hoặc 64")

def _fast_binary_conversion(fraction: float, max_bits: int) -> str:
    """Chuyển đổi phần thập phân sang nhị phân nhanh hơn sử dụng phép nhân 2."""
    result = []
    while fraction > 0 and len(result) < max_bits:
        fraction *= 2
        if fraction >= 1:
            result.append('1')
            fraction -= 1
        else:
            result.append('0')
    return ''.join(result)

@lru_cache(maxsize=1024)
def decimal_to_ieee754(num: float, bits: int = 32) -> Tuple[str, str]:
    """
    Chuyển đổi số thực sang dạng IEEE 754 (phiên bản tối ưu).
    """
    exp_bits, mantissa_bits, bias = _get_ieee_params(bits)
    explanation = [f"Chuyển đổi {num} sang IEEE 754 {bits}-bit:"]
    
    # Xử lý các trường hợp đặc biệt với lookup dictionary
    special_cases = {
        0: ('0' * bits, "Số 0 được biểu diễn bằng tất cả các bit 0"),
        float('inf'): ('0' + '1' * exp_bits + '0' * mantissa_bits, "Số dương vô cùng"),
        float('-inf'): ('1' + '1' * exp_bits + '0' * mantissa_bits, "Số âm vô cùng")
    }
    
    if num != num:  # NaN
        return ('0' + '1' * exp_bits + '1' + '0' * (mantissa_bits - 1),
                '\n'.join(explanation + ["Không phải là số (NaN)"]))
                
    if num in special_cases:
        return special_cases[num]

    # Xác định bit dấu và chuyển về số dương
    sign = '1' if num < 0 else '0'
    num = abs(num)
    explanation.append(f"1. Bit dấu: {sign} ({'âm' if num < 0 else 'dương'})")

    # Tối ưu việc tìm số mũ cho số >= 1
    if num >= 1:
        exp = floor(log2(num))
        mantissa_val = num / (2 ** exp) - 1
    else:
        exp = floor(log2(num))
        mantissa_val = (num / (2 ** exp)) - 1

    # Kiểm tra giới hạn số mũ
    biased_exp = exp + bias
    if biased_exp <= 0:
        return '0' * bits, '\n'.join(explanation + ["Số quá nhỏ, được biểu diễn là 0"])
    if biased_exp >= (1 << exp_bits) - 1:
        return (sign + '1' * exp_bits + '0' * mantissa_bits,
                '\n'.join(explanation + ["Số quá lớn, được biểu diễn là vô cùng"]))

    # Tính mantissa
    mantissa = _fast_binary_conversion(mantissa_val, mantissa_bits)
    mantissa = (mantissa + '0' * mantissa_bits)[:mantissa_bits]
    
    # Tạo kết quả
    biased_exp_binary = format(biased_exp, f'0{exp_bits}b')
    result = sign + biased_exp_binary + mantissa
    
    # Tạo giải thích
    explanation.extend([
        f"2. Số mũ thực: {exp}",
        f"3. Số mũ bias (E = e + {bias}): {biased_exp}",
        f"4. Số mũ nhị phân: {biased_exp_binary}",
        f"5. Mantissa: {mantissa}",
        f"\nKết quả: {result}",
        f"- Bit dấu (1 bit): {sign}",
        f"- Số mũ ({exp_bits} bits): {biased_exp_binary}",
        f"- Mantissa ({mantissa_bits} bits): {mantissa}"
    ])
    
    return result, '\n'.join(explanation)

@lru_cache(maxsize=1024)
def ieee754_to_decimal(binary: str) -> Tuple[float, str]:
    """
    Chuyển đổi số IEEE 754 sang số thực (phiên bản tối ưu).
    """
    bits = len(binary.strip())
    exp_bits, mantissa_bits, bias = _get_ieee_params(bits)
    
    # Validation nhanh với set
    if not set(binary).issubset({'0', '1'}):
        raise ValueError("Chuỗi nhị phân chỉ được chứa ký tự 0 và 1")

    explanation = [f"Chuyển đổi IEEE 754 {bits}-bit sang số thực:"]
    
    # Tách các phần
    sign_bit = binary[0]
    exp_bits_str = binary[1:exp_bits + 1]
    mantissa_bits_str = binary[exp_bits + 1:]
    
    explanation.extend([
        "1. Tách các thành phần:",
        f"   - Bit dấu: {sign_bit} ({'âm' if sign_bit == '1' else 'dương'})",
        f"   - Số mũ (biased): {exp_bits_str}",
        f"   - Mantissa: {mantissa_bits_str}"
    ])

    # Chuyển đổi số mũ nhanh hơn với int
    exp_val = int(exp_bits_str, 2)
    
    # Xử lý các trường hợp đặc biệt
    if exp_val == 0:
        if int(mantissa_bits_str, 2) == 0:
            return 0.0 if sign_bit == '0' else -0.0, '\n'.join(explanation + ["Số zero (±0)"])
    elif exp_val == (1 << exp_bits) - 1:
        if int(mantissa_bits_str, 2) == 0:
            return float('inf') if sign_bit == '0' else float('-inf'), '\n'.join(explanation + ["Số vô cùng (±∞)"])
        return float('nan'), '\n'.join(explanation + ["Không phải là số (NaN)"])

    # Tính toán mantissa hiệu quả hơn
    mantissa = 1.0 if exp_val != 0 else 0.0
    for i, bit in enumerate(mantissa_bits_str, 1):
        if bit == '1':
            mantissa += 2.0 ** -i

    # Tính kết quả cuối cùng
    exp = exp_val - bias if exp_val != 0 else 1 - bias
    result = (-1.0 if sign_bit == '1' else 1.0) * mantissa * (2.0 ** exp)
    
    explanation.extend([
        f"2. Số mũ thực = {exp_val} - {bias} = {exp}",
        f"3. Giá trị mantissa = {mantissa:.10f}",
        f"\nKết quả = {'-1' if sign_bit == '1' else '1'} × {mantissa:.10f} × 2^{exp} = {result}"
    ])
    
    return result, '\n'.join(explanation)

def handle_float_conversion_choice(message):
    chat_id = message.chat.id
    choice = message.text
    num_str = user_state[chat_id]['number']
    num = float(num_str)
    
    try:
        if choice == 'Chuyển sang nhị phân đơn giản':
            result, explanation = convert_float_to_binary(num_str)
        elif choice == 'Chuyển sang IEEE 754 (32-bit)':
            result, explanation = decimal_to_ieee754(num, 32)
        elif choice == 'Chuyển sang IEEE 754 (64-bit)':
            result, explanation = decimal_to_ieee754(num, 64)
        else:
            bot.reply_to(message, "Lựa chọn không hợp lệ")
            return
            
        response = explanation
        
        if len(response) > 4096:
            for x in range(0, len(response), 4096):
                bot.send_message(chat_id, response[x:x+4096])
        else:
            bot.reply_to(message, response, reply_markup=types.ReplyKeyboardRemove())
        
        conversion_history = f"{num_str} -> {result} ({choice})"
        db.update_convert_all(chat_id)
        db.add_conversion_history(chat_id, conversion_history)
        
        user_state[chat_id] = {'step': 'input_number'}
        bot.send_message(chat_id, "Bạn có thể bắt đầu một phép chuyển đổi mới bằng cách nhập một số khác.")
    except Exception as e:
        bot.reply_to(message, f"Có lỗi xảy ra: {str(e)}")
        user_state[chat_id] = {'step': 'input_number'}

bot.polling(none_stop=True)