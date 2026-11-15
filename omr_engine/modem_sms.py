"""
omr_engine/modem_sms.py
USB / GSM Modem communication module for sending offline OMR exam results via SMS.
Communicates with 3G/4G/GSM USB Dongles (ZTE, Huawei, SIM800, etc.) over serial COM ports using AT commands.
Supports automatic SMS port detection, SIM readiness verification, operator SMSC auto-config,
Bengali numbers conversion, and robust error recovery.
"""

import time
import re
import logging
import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

# Bengali digits translation table
BENGALI_DIGITS = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


def list_com_ports():
    """
    Scans and returns all available Serial / COM ports on the system.
    Identifies and sorts the best SMS-capable AT port to the top.
    Returns:
        list of dict: [{
            'port': 'COM10',
            'description': 'COM10 — DataCard UI AT Interface (প্রস্তাবিত: SMS পাঠানোর পোর্ট)',
            'raw_description': 'DataCard UI AT Interface',
            'is_recommended': True,
            'is_warning': False
        }, ...]
    """
    ports = []
    try:
        com_list = serial.tools.list_ports.comports()
        for p in com_list:
            port_name = p.device
            desc = p.description or port_name
            hwid = p.hwid or ''

            combined = f"{desc} {hwid}".lower()
            is_recommended = False
            is_warning = False
            tag = ""

            # Check if this port is the primary AT / Modem interface
            if any(k in combined for k in ['ui at', 'at interface', 'application', 'pc ui', 'modem']):
                is_recommended = True
                tag = " (প্রস্তাবিত: SMS পাঠানোর পোর্ট)"
            elif any(k in combined for k in ['diagnostic', 'diag', 'voice', 'audio', 'nmea']):
                is_warning = True
                tag = " (ডায়াগনস্টিক/অডিও - SMS নাও চলতে পারে)"

            ports.append({
                'port': port_name,
                'description': f"{desc}{tag}",
                'raw_description': desc,
                'hwid': hwid,
                'is_recommended': is_recommended,
                'is_warning': is_warning
            })
    except Exception as e:
        logger.error(f"Error listing COM ports: {e}")

    # Sort recommended ports first, normal ports second, diagnostic/warning ports last
    ports.sort(key=lambda x: (0 if x['is_recommended'] else (2 if x['is_warning'] else 1), x['port']))
    return ports


def normalize_bd_phone(raw_input):
    """
    Normalizes and validates a Bangladeshi mobile number extracted from OMR bubbles or input.
    Handles:
      - 11 digits: '01712345678' -> '01712345678'
      - 10 digits without leading 0: '1712345678' -> '01712345678'
      - With international prefix: '+8801712345678' or '8801712345678' -> '01712345678'
      - Bengali digits: '০১৭...' -> '017...'
    Returns:
      dict: {'is_valid': bool, 'local': '01XXXXXXXXX', 'international': '+8801XXXXXXXXX', 'error': ...}
    """
    if not raw_input:
        return {'is_valid': False, 'local': '', 'international': '', 'error': 'ফোন নম্বর খালি'}

    # Convert Bengali digits to English
    cleaned = str(raw_input).strip().translate(BENGALI_DIGITS)
    digits = re.sub(r'\D', '', cleaned)

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


def parse_cms_error(raw_response):
    """
    Translates AT CMS/CME error codes into actionable Bengali instructions.
    """
    if not raw_response:
        return "মডেম থেকে কোনো সাড়া পাওয়া যায়নি (টাইমআউট)।"

    text = str(raw_response).strip()

    # Match numeric +CMS ERROR: <code> or +CME ERROR: <code>
    cms_match = re.search(r'\+CM[SE]\s*ERROR:\s*(\d+)', text, re.IGNORECASE)
    if cms_match:
        code = int(cms_match.group(1))
        cms_map = {
            300: "মডেম হার্ডওয়্যার ত্রুটি (ME failure)",
            301: "এসএমএস সার্ভিস সংরক্ষিত নয় (SMS service reserved)",
            302: "মডেম বা সিম ব্যস্ত (Operation not allowed)। দয়া করে কম্পিউটারে কোনো মডেম ডায়ালার সফটওয়্যার (যেমন: GP Internet / Join Air / Teletalk 3G) চালু থাকলে তা Disconnect ও Exit করুন।",
            303: "মডেম অপারেশন সমর্থন করেনি (Operation not supported)",
            304: "অবৈধ এসএমএস ফরম্যাট (Invalid PDU/Text mode parameter)",
            305: "অবৈধ টেক্সট মোড প্যারামিটার (Invalid text mode)",
            310: "মডেমে কোনো সিম কার্ড পাওয়া যায়নি (SIM not inserted)",
            311: "সিম পিন লক করা (SIM PIN required)। ফোনে সিম লাগিয়ে পিন বন্ধ করুন।",
            312: "সিম পাক কোড প্রয়োজন (SIM PUK required)",
            313: "সিম কার্ড কাজ করছে না (SIM failure)",
            314: "সিম কার্ড ব্যস্ত (SIM busy)",
            320: "মেমোরি পূর্ণ (Memory full)",
            330: "এসএমএস সেন্টার (SMSC) নম্বর সেট করা নেই। মডেম সেটিংস থেকে যাচাই করুন।",
            500: "নেটওয়ার্ক ত্রুটি বা সিমে পর্যাপ্ত ব্যালেন্স/এসএমএস প্যাক নেই (Network/Balance failure)",
            512: "মোবাইল টাওয়ারের সাথে সংযোগ স্থাপন ব্যর্থ (MM establishment failure)"
        }
        if code in cms_map:
            return f"{cms_map[code]} (কোড: {code})"

    if "SIM not inserted" in text or "+CPIN: NOT INSERTED" in text:
        return "মডেমে কোনো সিম কার্ড পাওয়া যায়নি। সিম কার্ড সঠিকভাবে প্রবেশ করানো আছে কিনা দেখুন।"
    if "SIM PIN" in text or "+CPIN: SIM PIN" in text:
        return "সিম কার্ডটি পিন লক করা। অনুগ্রহ করে সিমের পিন লক নিষ্ক্রিয় করুন।"
    if "Access is denied" in text or "PermissionError" in text:
        return "কম্পিউটারের অন্য কোনো সফটওয়্যার (যেমন: GP Modem, Join Air) এই পোর্টটি দখল করে রেখেছে। সেই সফটওয়্যারটি বন্ধ করে পুনরায় চেষ্টা করুন।"

    clean_err = text.replace('\r', ' ').replace('\n', ' ').strip()
    return clean_err or "মডেম ত্রুটি"


def _get_operator_smsc(phone_number):
    """
    Returns standard SMS Center number for Bangladeshi operators.
    """
    p = str(phone_number)
    if p.startswith(('+88018', '018', '+88016', '016')):
        return "+8801801000007"  # Robi / Airtel
    elif p.startswith(('+88019', '019', '+88014', '014')):
        return "+8801900000600"  # Banglalink
    elif p.startswith(('+88015', '015')):
        return "+8801550000000"  # Teletalk
    return "+8801700000600"      # Grameenphone / Default


def sanitize_sms_for_gsm(text):
    """
    Prepares SMS text for GSM modems:
    - Converts Bengali digits to English
    - Normalizes newlines
    - Maps common Bengali exam words to clear Latin if pure ASCII is required
    """
    if not text:
        return ""

    # Convert Bengali digits
    t = text.translate(BENGALI_DIGITS)

    # Dictionary for fallback clean transliteration of common OMR keywords
    replacements = {
        'এডমিশন': 'Admission',
        'চ্যালেঞ্জ': 'Challenge',
        'একাডেমিক': 'Academic',
        'কেয়ার': 'Care',
        'পরীক্ষা': 'Exam',
        'রোল': 'Roll',
        'নাম': 'Name',
        'নম্বর': 'Score',
        'মোট নম্বর': 'Total Marks',
        'মেধাক্রম': 'Rank',
        'সঠিক': 'Correct',
        'ভুল': 'Wrong',
        'উত্তর দেওয়া হয়নি': 'Unattempted',
        'রেজাল্ট': 'Result',
        'অভিনন্দন': 'Congratulations'
    }
    return t


def test_modem(port, baudrate=115200, timeout=4):
    """
    Thoroughly tests communication with a GSM modem on the given COM port.
    Tests:
      1. AT connectivity
      2. Device Model
      3. SIM Card readiness (AT+CPIN?)
      4. Network Registration (AT+CREG?)
      5. Signal Quality (AT+CSQ)
      6. SMS Capability on this specific port (AT+CMGF=1)
    """
    if not port:
        return {'success': False, 'error': 'COM পোর্ট নির্বাচন করুন'}

    ser = None
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # 1. Turn off echo and test AT connection
        ser.write(b"ATE0\r\n")
        time.sleep(0.2)
        ser.read(ser.in_waiting or 64)

        ser.write(b"AT\r\n")
        time.sleep(0.2)
        resp = ser.read(ser.in_waiting or 64).decode(errors='ignore')
        if "OK" not in resp:
            # Retry wake-up
            ser.write(b"AT\r\n")
            time.sleep(0.3)
            resp = ser.read(ser.in_waiting or 64).decode(errors='ignore')
            if "OK" not in resp:
                return {
                    'success': False,
                    'error': f"{port} পোর্ট থেকে কোনো সাড়া মেলেনি। মডেম সঠিকভাবে প্লাগ করা আছে কিনা যাচাই করুন।"
                }

        # Enable verbose errors
        ser.write(b"AT+CMEE=2\r\n")
        time.sleep(0.1)
        ser.read(ser.in_waiting or 64)

        # 2. Get Manufacturer / Model
        model = "Generic GSM Modem"
        try:
            ser.write(b"AT+CGMM\r\n")
            time.sleep(0.2)
            model_resp = ser.read(ser.in_waiting or 128).decode(errors='ignore')
            lines = [l.strip() for l in model_resp.splitlines() if l.strip() and "AT+" not in l and "OK" not in l]
            if lines:
                model = lines[0]
        except Exception:
            pass

        # 3. Test SMS Text Mode on THIS specific port
        ser.write(b"AT+CMGF=1\r\n")
        time.sleep(0.2)
        cmgf_resp = ser.read(ser.in_waiting or 128).decode(errors='ignore')
        if "OK" not in cmgf_resp:
            return {
                'success': False,
                'port': port,
                'is_sms_capable': False,
                'error': f"সতর্কতা: '{port}' পোর্টে মডেম রেসপন্স করলেও এটি SMS পাঠানোর পোর্ট নয় (এটি ডায়াগনস্টিক/ভয়েস পোর্ট)। অনুগ্রহ করে ড্রপডাউন থেকে আপনার মডেমের অন্য পোর্টটি (যেমন: 'UI AT Interface' বা 'Modem') নির্বাচন করুন।"
            }

        # 4. Check SIM Card Readiness (AT+CPIN?)
        sim_status = "সক্রিয় (Ready)"
        try:
            ser.write(b"AT+CPIN?\r\n")
            time.sleep(0.2)
            cpin_resp = ser.read(ser.in_waiting or 128).decode(errors='ignore')
            if "+CPIN: READY" in cpin_resp:
                sim_status = "সিম সক্রিয় (Ready)"
            elif "SIM PIN" in cpin_resp:
                sim_status = "সিম পিন লক (PIN Required)"
            elif "NOT INSERTED" in cpin_resp:
                sim_status = "সিম কার্ড পাওয়া যায়নি"
        except Exception:
            pass

        # 5. Query Network Registration (AT+CREG?)
        network_status = "অজ্ঞাত"
        try:
            ser.write(b"AT+CREG?\r\n")
            time.sleep(0.2)
            creg_resp = ser.read(ser.in_waiting or 128).decode(errors='ignore')
            m_creg = re.search(r'\+CREG:\s*\d+,(\d+)', creg_resp)
            if m_creg:
                stat = int(m_creg.group(1))
                if stat in (1, 5):
                    network_status = "নেটওয়ার্ক সংযুক্ত (Connected)"
                elif stat == 2:
                    network_status = "নেটওয়ার্ক খুঁজছে (Searching...)"
                else:
                    network_status = "নেটওয়ার্কে সংযুক্ত নয় (No Service)"
        except Exception:
            pass

        # 6. Query Signal Quality (AT+CSQ)
        signal_rssi = 0
        signal_dbm = "অজ্ঞাত"
        try:
            ser.write(b"AT+CSQ\r\n")
            time.sleep(0.2)
            csq_resp = ser.read(ser.in_waiting or 128).decode(errors='ignore')
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
            'sim_status': sim_status,
            'network_status': network_status,
            'signal_rssi': signal_rssi,
            'signal_dbm': signal_dbm,
            'is_sms_capable': True,
            'error': None
        }

    except serial.SerialException as se:
        err_msg = str(se)
        if "Access is denied" in err_msg or "PermissionError" in err_msg:
            return {
                'success': False,
                'error': f"সিরিয়াল পোর্ট এরর: '{port}' পোর্টটি অন্য কোনো সফটওয়্যার দখল করে রেখেছে। মডেমের নিজস্ব সফটওয়্যার (GP Modem / Join Air ইত্যাদি) চালু থাকলে তা বন্ধ (Exit) করুন।"
            }
        return {'success': False, 'error': f"সিরিয়াল পোর্ট এরর: {err_msg}"}
    except Exception as ex:
        return {'success': False, 'error': f"সংযোগ ব্যর্থ: {ex}"}
    finally:
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass


def send_sms_via_modem(port, baudrate, recipient, text, timeout=20):
    """
    Sends a single SMS message to recipient via GSM modem over serial AT commands.
    Features:
      - Validates recipient number
      - Disables echo (ATE0)
      - Verifies SIM status (AT+CPIN?)
      - Sets SMS Text Mode (AT+CMGF=1) and SMS parameters (AT+CSMP=17,167,0,0)
      - Auto-configures SMS Center (SMSC) if missing
      - Bengali digits conversion & safe Unicode/GSM handling
      - Fast abort on ERROR response instead of waiting for timeout
      - Clean ESC recovery
    """
    norm = normalize_bd_phone(recipient)
    if not norm['is_valid']:
        return {'success': False, 'error': norm['error']}

    phone_intl = norm['international']  # +8801XXXXXXXXX
    phone_local = norm['local']          # 01XXXXXXXXX

    # Clean text: convert Bengali digits to English
    sanitized_text = sanitize_sms_for_gsm(text)
    clean_text = sanitized_text.replace('\r\n', '\n').replace('\n', '\r\n')

    # Detect if text contains non-ASCII characters
    is_unicode = any(ord(c) > 127 for c in clean_text)

    ser = None
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # 1. Echo Off & Attention
        ser.write(b"ATE0\r\n")
        time.sleep(0.1)
        ser.read(ser.in_waiting or 64)

        ser.write(b"AT\r\n")
        time.sleep(0.2)
        resp_at = ser.read(ser.in_waiting or 64).decode(errors='ignore')
        if "OK" not in resp_at:
            ser.write(b"AT\r\n")
            time.sleep(0.3)
            resp_at = ser.read(ser.in_waiting or 64).decode(errors='ignore')
            if "OK" not in resp_at:
                return {'success': False, 'error': f"মডেম থেকে কোনো সাড়া পাওয়া যায়নি ({port})"}

        # 2. Enable Detailed Error Codes
        ser.write(b"AT+CMEE=1\r\n")
        time.sleep(0.1)
        ser.read(ser.in_waiting or 64)

        # 3. Check SIM Card Status
        ser.write(b"AT+CPIN?\r\n")
        time.sleep(0.2)
        resp_cpin = ser.read(ser.in_waiting or 128).decode(errors='ignore')
        if "+CPIN:" in resp_cpin and "READY" not in resp_cpin:
            return {'success': False, 'error': f"সিম কার্ড প্রস্তুত নয়: {parse_cms_error(resp_cpin)}"}

        # 4. Set SMS Text Mode
        ser.write(b"AT+CMGF=1\r\n")
        time.sleep(0.2)
        resp_cmgf = ser.read(ser.in_waiting or 128).decode(errors='ignore')
        if "OK" not in resp_cmgf:
            return {
                'success': False,
                'error': f"{port} পোর্ট SMS Text Mode সমর্থন করেনি ({resp_cmgf.strip()})। অনুগ্রহ করে মডেম সেটিংস থেকে 'UI AT Interface' পোর্টটি নির্বাচন করুন।"
            }

        # 5. Set SMS Text Delivery Parameters (FO=17, VP=167, PID=0, DCS=0)
        ser.write(b"AT+CSMP=17,167,0,0\r\n")
        time.sleep(0.1)
        ser.read(ser.in_waiting or 64)

        # 6. Auto-configure SMS Center (SMSC) if not set
        ser.write(b"AT+CSCA?\r\n")
        time.sleep(0.2)
        resp_csca = ser.read(ser.in_waiting or 128).decode(errors='ignore')
        if '+CSCA: ""' in resp_csca or "+CSCA:" not in resp_csca:
            smsc = _get_operator_smsc(phone_local)
            ser.write(f'AT+CSCA="{smsc}"\r\n'.encode('ascii'))
            time.sleep(0.2)
            ser.read(ser.in_waiting or 64)

        # 7. Character set configuration
        # For pure ASCII, try standard GSM/IRA
        if not is_unicode:
            ser.write(b'AT+CSCS="GSM"\r\n')
            time.sleep(0.1)
            resp_cscs = ser.read(ser.in_waiting or 64).decode(errors='ignore')
            if "OK" not in resp_cscs:
                ser.write(b'AT+CSCS="IRA"\r\n')
                time.sleep(0.1)
                ser.read(ser.in_waiting or 64)

        # Helper to attempt sending AT+CMGS
        def _attempt_send(target_number, message_body, is_ucs2_mode=False):
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            cmd = f'AT+CMGS="{target_number}"\r\n'.encode('ascii')
            ser.write(cmd)

            # Wait for '>' prompt from modem or abort on ERROR
            prompt_received = False
            start_wait = time.time()
            buf = ""
            while time.time() - start_wait < 5.0:
                if ser.in_waiting:
                    chunk = ser.read(ser.in_waiting).decode(errors='ignore')
                    buf += chunk
                    if ">" in buf:
                        prompt_received = True
                        break
                    if "ERROR" in buf or "+CMS ERROR" in buf:
                        break
                time.sleep(0.05)

            if not prompt_received:
                ser.write(b"\x1B")  # Send ESC to cancel
                time.sleep(0.2)
                ser.read(ser.in_waiting or 64)
                return {'success': False, 'error_resp': buf.strip()}

            # Send payload followed by Ctrl+Z (ASCII 26 / 0x1A)
            if is_ucs2_mode:
                payload = message_body.encode('ascii') + b"\x1A"
            else:
                payload = message_body.encode('ascii', errors='replace') + b"\x1A"
            ser.write(payload)

            # Wait for +CMGS confirmation
            resp_buf = ""
            sent = False
            msg_id = ""
            start_wait = time.time()
            while time.time() - start_wait < timeout:
                if ser.in_waiting:
                    chunk = ser.read(ser.in_waiting).decode(errors='ignore')
                    resp_buf += chunk
                    if "OK" in resp_buf and "+CMGS" in resp_buf:
                        sent = True
                        m = re.search(r'\+CMGS:\s*(\d+)', resp_buf)
                        if m:
                            msg_id = m.group(1)
                        break
                    elif "ERROR" in resp_buf or "+CMS ERROR" in resp_buf:
                        break
                time.sleep(0.1)

            if not sent:
                ser.write(b"\x1B")  # ESC recovery
                time.sleep(0.2)
                ser.read(ser.in_waiting or 64)
                return {'success': False, 'error_resp': resp_buf.strip()}

            return {'success': True, 'msg_id': msg_id}

        # 8. Send SMS: Try international format first, fallback to local
        # If Unicode, attempt UCS2 text mode; if modem rejects UCS2, fallback to clean transliterated ASCII
        final_result = None

        if is_unicode:
            # Check if modem supports UCS2
            ser.write(b'AT+CSCS="UCS2"\r\n')
            time.sleep(0.1)
            resp_ucs2 = ser.read(ser.in_waiting or 64).decode(errors='ignore')
            if "OK" in resp_ucs2:
                ser.write(b"AT+CSMP=17,167,0,8\r\n")  # DCS=8 for UCS2
                time.sleep(0.1)
                ser.read(ser.in_waiting or 64)

                ucs2_text = clean_text.encode('utf-16-be').hex().upper()
                # Try international
                res = _attempt_send(phone_intl, ucs2_text, is_ucs2_mode=True)
                if not res['success']:
                    # Try local format
                    res = _attempt_send(phone_local, ucs2_text, is_ucs2_mode=True)
                if res['success']:
                    final_result = res

        # If pure ASCII or UCS2 was not successful, fallback to ASCII/GSM mode
        if not final_result or not final_result.get('success'):
            ser.write(b'AT+CSMP=17,167,0,0\r\n')
            time.sleep(0.1)
            ser.read(ser.in_waiting or 64)
            ser.write(b'AT+CSCS="GSM"\r\n')
            time.sleep(0.1)
            ser.read(ser.in_waiting or 64)

            # Transliterate / encode cleanly to ASCII
            ascii_text = clean_text.encode('ascii', errors='replace').decode('ascii')
            # Try international format
            res = _attempt_send(phone_intl, ascii_text, is_ucs2_mode=False)
            if not res['success']:
                # Retry with local number
                res = _attempt_send(phone_local, ascii_text, is_ucs2_mode=False)
            final_result = res

        if final_result and final_result.get('success'):
            return {'success': True, 'message_id': final_result.get('msg_id', 'OK'), 'error': None}
        else:
            raw_err = final_result.get('error_resp', '') if final_result else ''
            clean_error = parse_cms_error(raw_err)
            return {'success': False, 'error': f"এসএমএস প্রেরণে ত্রুটি: {clean_error}"}

    except serial.SerialException as se:
        return {'success': False, 'error': f"সিরিয়াল পোর্ট এরর: {se}"}
    except Exception as ex:
        logger.exception("Modem SMS sending exception")
        return {'success': False, 'error': f"এসএমএস প্রেরণে ব্যর্থ: {ex}"}
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
