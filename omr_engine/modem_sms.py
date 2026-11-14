"""
omr_engine/modem_sms.py
USB / GSM Modem communication module for sending offline OMR exam results via SMS.
Communicates with 3G/4G/GSM USB Dongles (Huawei, ZTE, SIM800, etc.) over serial COM ports using AT commands.
"""

import time
import re
import logging
import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


def list_com_ports():
    """
    Scans and returns all available Serial / COM ports on the system.
    Returns:
        list of dict: [{'port': 'COM3', 'description': 'HUAWEI Mobile Connect...', 'hwid': '...'}]
    """
    ports = []
    try:
        com_list = serial.tools.list_ports.comports()
        for p in com_list:
            ports.append({
                'port': p.device,
                'description': p.description or p.device,
                'hwid': p.hwid or ''
            })
    except Exception as e:
        logger.error(f"Error listing COM ports: {e}")
    return ports


def normalize_bd_phone(raw_input):
    """
    Normalizes and validates a Bangladeshi mobile number extracted from OMR bubbles or input.
    Handles:
      - 11 digits: '01712345678' -> '01712345678'
      - 10 digits without leading 0: '1712345678' -> '01712345678'
      - With international prefix: '+8801712345678' or '8801712345678' -> '01712345678'
    Returns:
      dict: {'is_valid': bool, 'local': '01XXXXXXXXX', 'international': '+8801XXXXXXXXX', 'error': ...}
    """
    if not raw_input:
        return {'is_valid': False, 'local': '', 'international': '', 'error': 'ফোন নম্বর খালি'}

    digits = re.sub(r'\D', '', str(raw_input).strip())

    # Strip country code 88 if present
    if digits.startswith('88') and len(digits) >= 12:
        digits = digits[2:]

    # Prepend 0 if 10 digits starting with 1
    if len(digits) == 10 and digits.startswith('1'):
        digits = '0' + digits

    # BD operators: 013, 014, 015, 016, 017, 018, 019
    valid_prefixes = ('013', '014', '015', '016', '017', '018', '019')
    if len(digits) == 11 and digits.startswith(valid_prefixes):
        return {
            'is_valid': True,
            'local': digits,
            'international': f"+88{digits}",
            'error': None
        }

    return {
        'is_valid': False,
        'local': digits,
        'international': f"+88{digits}" if digits else '',
        'error': f"অকার্যকর মোবাইল নম্বর: '{raw_input}'"
    }


def test_modem(port, baudrate=115200, timeout=3):
    """
    Tests communication with a GSM modem on the given COM port.
    Sends standard AT commands to query device model and signal quality.
    """
    if not port:
        return {'success': False, 'error': 'COM পোর্ট নির্বাচন করুন'}

    ser = None
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        time.sleep(0.5)

        # 1. Test AT connection
        ser.write(b"AT\r\n")
        resp = ser.read(64).decode(errors='ignore')
        if "OK" not in resp:
            # Try alternate baudrate or wake-up
            ser.write(b"AT\r\n")
            resp = ser.read(64).decode(errors='ignore')
            if "OK" not in resp:
                return {'success': False, 'error': f"মডেম থেকে কোনো সাড়া মেলেনি ({port})"}

        # 2. Get Manufacturer / Model
        model = "Generic GSM Modem"
        try:
            ser.write(b"AT+CGMM\r\n")
            model_resp = ser.read(128).decode(errors='ignore')
            lines = [l.strip() for l in model_resp.splitlines() if l.strip() and "AT+" not in l and "OK" not in l]
            if lines:
                model = lines[0]
        except Exception:
            pass

        # 3. Query Signal Quality (AT+CSQ)
        signal_rssi = 0
        signal_dbm = "অজ্ঞাত"
        try:
            ser.write(b"AT+CSQ\r\n")
            csq_resp = ser.read(128).decode(errors='ignore')
            # Response like +CSQ: 18,99
            match = re.search(r'\+CSQ:\s*(\d+),', csq_resp)
            if match:
                rssi = int(match.group(1))
                signal_rssi = rssi
                if rssi == 99:
                    signal_dbm = "সিগন্যাল নেই (99)"
                elif rssi >= 20:
                    signal_dbm = f"খুব ভালো ({rssi}/31)"
                elif rssi >= 15:
                    signal_dbm = f"ভালো ({rssi}/31)"
                elif rssi >= 10:
                    signal_dbm = f"মাঝারি ({rssi}/31)"
                else:
                    signal_dbm = f"দুর্বল ({rssi}/31)"
        except Exception:
            pass

        return {
            'success': True,
            'port': port,
            'model': model,
            'signal_rssi': signal_rssi,
            'signal_dbm': signal_dbm,
            'error': None
        }

    except serial.SerialException as se:
        return {'success': False, 'error': f"সিরিয়াল পোর্ট এরর: {se}"}
    except Exception as ex:
        return {'success': False, 'error': f"সংযোগ ব্যর্থ: {ex}"}
    finally:
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass


def send_sms_via_modem(port, baudrate, recipient, text, timeout=15):
    """
    Sends a single SMS message to recipient via GSM modem over serial AT commands.
    Args:
        port (str): COM port name, e.g. 'COM3'
        baudrate (int): Baud rate, default 115200
        recipient (str): Mobile number ('017XXXXXXXX' or '+88017XXXXXXXX')
        text (str): SMS text content
        timeout (int): Timeout in seconds
    Returns:
        dict: {'success': bool, 'message_id': str, 'error': str}
    """
    norm = normalize_bd_phone(recipient)
    if not norm['is_valid']:
        return {'success': False, 'error': norm['error']}

    phone_to_send = norm['international']  # E.g. +8801712345678

    ser = None
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        time.sleep(0.3)

        # 1. Attention & Reset
        ser.write(b"AT\r\n")
        time.sleep(0.3)
        ser.read(128)

        # 2. Set SMS Text Mode
        ser.write(b"AT+CMGF=1\r\n")
        time.sleep(0.3)
        resp_cmgf = ser.read(128).decode(errors='ignore')
        if "OK" not in resp_cmgf:
            return {'success': False, 'error': 'মডেম Text Mode (AT+CMGF=1) সমর্থন করেনি'}

        # 3. Set GSM Character Set
        ser.write(b'AT+CSCS="GSM"\r\n')
        time.sleep(0.2)
        ser.read(64)

        # 4. Initiate SMS send
        cmgs_cmd = f'AT+CMGS="{phone_to_send}"\r\n'.encode('ascii')
        ser.write(cmgs_cmd)
        
        # Wait for '>' prompt from modem
        prompt_received = False
        start_time = time.time()
        buf = ""
        while time.time() - start_time < 5:
            if ser.in_waiting:
                ch = ser.read().decode(errors='ignore')
                buf += ch
                if ">" in buf:
                    prompt_received = True
                    break
            time.sleep(0.05)

        if not prompt_received:
            # Cancel command
            ser.write(b"\x1B") # ESC
            return {'success': False, 'error': f"মডেম থেকে '>' প্রম্পট পাওয়া যায়নি (রেসপন্স: {buf.strip()})"}

        # 5. Send message payload followed by Ctrl+Z (ASCII 26 / 0x1A)
        # Note: Replace newlines with standard CRLF
        clean_text = text.replace('\r\n', '\n').replace('\n', '\r\n')
        payload = clean_text.encode('utf-8', errors='ignore') + b"\x1A"
        ser.write(payload)

        # 6. Wait for +CMGS confirmation or ERROR
        resp_buf = ""
        sent_success = False
        msg_id = ""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting).decode(errors='ignore')
                resp_buf += chunk
                if "OK" in resp_buf and "+CMGS" in resp_buf:
                    sent_success = True
                    match = re.search(r'\+CMGS:\s*(\d+)', resp_buf)
                    if match:
                        msg_id = match.group(1)
                    break
                elif "ERROR" in resp_buf or "+CMS ERROR" in resp_buf:
                    break
            time.sleep(0.1)

        if sent_success:
            return {'success': True, 'message_id': msg_id, 'error': None}
        else:
            clean_err = resp_buf.strip().replace('\r', ' ').replace('\n', ' ')
            return {'success': False, 'error': f"এসএমএস প্রেরণে ত্রুটি: {clean_err or 'টাইমআউট'}"}

    except serial.SerialException as se:
        return {'success': False, 'error': f"সিরিয়াল পোর্ট এরর: {se}"}
    except Exception as ex:
        return {'success': False, 'error': f"ব্যর্থ: {ex}"}
    finally:
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass


def render_sms_template(template_str, exam, result, institute=None):
    """
    Renders dynamic tags inside the SMS template for a student result.
    Tags:
      [INSTITUTE_NAME], [EXAM_TITLE], [NAME], [ROLL], [MOBILE],
      [MARKS], [TOTAL_Q], [RANK], [CORRECT], [WRONG], [UNATTEMPTED]
    """
    text = template_str or ""
    inst_name = institute.institute_name if institute else "OMR Evaluation"

    # Determine phone/mobile string
    mob_str = result.phone_number or result.roll_no or ""

    replacements = {
        '[INSTITUTE_NAME]': inst_name,
        '[EXAM_TITLE]': exam.title,
        '[NAME]': result.student_name or f"Roll {result.roll_no}",
        '[ROLL]': result.roll_no,
        '[MOBILE]': mob_str,
        '[MARKS]': str(round(result.marks, 2)),
        '[TOTAL_Q]': str(exam.total_questions),
        '[RANK]': str(result.rank or '-'),
        '[CORRECT]': str(result.correct_count),
        '[WRONG]': str(result.wrong_count),
        '[UNATTEMPTED]': str(result.unattempted_count),
    }

    for tag, val in replacements.items():
        text = text.replace(tag, val)

    return text.strip()
