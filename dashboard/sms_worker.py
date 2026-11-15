"""
dashboard/sms_worker.py
Thread-safe background SMS campaign manager for USB GSM Modem.
Enables asynchronous background bulk SMS sending so users can freely navigate
to other pages or close the browser without interrupting ongoing SMS deliveries.
"""

import time
import threading
import logging
from datetime import datetime
from django.utils import timezone
from django.db import connection

logger = logging.getLogger(__name__)

# In-memory registry of active and recent SMS campaigns by exam_id
_CAMPAIGNS = {}
_LOCK = threading.Lock()


def get_campaign_status(exam_id):
    """Returns the current or most recent SMS campaign status for an exam."""
    with _LOCK:
        camp = _CAMPAIGNS.get(exam_id)
        if not camp:
            return None
        # Return a copy to avoid mutation during JSON serialization
        return {
            'is_running': camp.get('is_running', False),
            'stop_requested': camp.get('stop_requested', False),
            'exam_id': camp.get('exam_id'),
            'exam_title': camp.get('exam_title', ''),
            'total': camp.get('total', 0),
            'current_index': camp.get('current_index', 0),
            'sent_count': camp.get('sent_count', 0),
            'failed_count': camp.get('failed_count', 0),
            'remaining_count': max(0, camp.get('total', 0) - camp.get('current_index', 0)),
            'current_phone': camp.get('current_phone', ''),
            'current_roll': camp.get('current_roll', ''),
            'percent': int((camp.get('current_index', 0) / max(1, camp.get('total', 1))) * 100),
            'logs': list(camp.get('logs', []))[-50:],  # last 50 log messages
            'started_at': camp.get('started_at'),
            'finished_at': camp.get('finished_at'),
        }


def stop_exam_sms_campaign(exam_id):
    """Requests an ongoing SMS campaign to stop gracefully."""
    with _LOCK:
        camp = _CAMPAIGNS.get(exam_id)
        if camp and camp.get('is_running'):
            camp['stop_requested'] = True
            camp['logs'].append({
                'time': datetime.now().strftime('%I:%M:%S %p'),
                'type': 'warning',
                'msg': 'এসএমএস প্রেরণ স্থগিত করার অনুরোধ গ্রহণ করা হয়েছে।'
            })
            return True
        return False


def start_exam_sms_campaign(exam_id, filter_option='pending'):
    """
    Spawns a background thread to process the SMS queue for the given exam.
    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    from .models import StandaloneExam, StandaloneResult, ModemSettings, InstituteSettings, SmsLog
    from omr_engine.modem_sms import normalize_bd_phone, render_sms_template, send_sms_via_modem

    with _LOCK:
        camp = _CAMPAIGNS.get(exam_id)
        if camp and camp.get('is_running'):
            return False, 'এই পরীক্ষার জন্য ইতিমধ্যে ব্যাকগ্রাউন্ডে এসএমএস প্রেরণ চলমান রয়েছে।'

    try:
        exam = StandaloneExam.objects.get(id=exam_id)
    except StandaloneExam.DoesNotExist:
        return False, 'পরীক্ষাটি পাওয়া যায়নি।'

    modem_settings = ModemSettings.get_settings()
    if not modem_settings.com_port:
        return False, 'মডেমের COM পোর্ট কনফিগার করা নেই। অনুগ্রহ করে সেটিংস থেকে পোর্ট সিলেক্ট করুন।'

    institute = InstituteSettings.get_settings()

    # Determine targets
    results_qs = exam.results.all().order_by('rank', '-marks')
    target_results = []

    for r in results_qs:
        target_phone = r.phone_number or r.roll_no
        norm = normalize_bd_phone(target_phone)
        if not norm['is_valid']:
            continue
        if filter_option == 'pending' and r.sms_status == 'sent':
            continue
        target_results.append(r.id)

    if not target_results:
        return False, 'প্রেরণ করার মতো কোনো উপযুক্ত মোবাইল নম্বর পাওয়া যায়নি।'

    # Initialize campaign record
    with _LOCK:
        _CAMPAIGNS[exam_id] = {
            'is_running': True,
            'stop_requested': False,
            'exam_id': exam.id,
            'exam_title': exam.title,
            'total': len(target_results),
            'current_index': 0,
            'sent_count': 0,
            'failed_count': 0,
            'current_phone': '',
            'current_roll': '',
            'logs': [{
                'time': datetime.now().strftime('%I:%M:%S %p'),
                'type': 'info',
                'msg': f"ব্যাকগ্রাউন্ডে মোট {len(target_results)} জন শিক্ষার্থীর এসএমএস প্রেরণ শুরু হয়েছে..."
            }],
            'started_at': datetime.now().strftime('%d %b %Y, %I:%M:%S %p'),
            'finished_at': None
        }

    # Worker function inside daemon thread
    def _worker(exam_id_val, result_ids_list, port_val, baud_val, delay_val, template_val):
        # Close old connection to avoid thread-sharing issues with SQLite
        connection.close()

        logger.info(f"[SMS Campaign {exam_id_val}] Background worker started for {len(result_ids_list)} items.")
        sent_local = 0
        failed_local = 0

        for idx, res_id in enumerate(result_ids_list):
            with _LOCK:
                camp = _CAMPAIGNS.get(exam_id_val)
                if not camp or camp.get('stop_requested'):
                    logger.info(f"[SMS Campaign {exam_id_val}] Stopped by user request at item {idx}.")
                    break

            try:
                result = StandaloneResult.objects.select_related('exam').get(id=res_id)
                target_phone = result.phone_number or result.roll_no
                norm = normalize_bd_phone(target_phone)

                with _LOCK:
                    camp['current_index'] = idx + 1
                    camp['current_phone'] = norm['local'] if norm['is_valid'] else target_phone
                    camp['current_roll'] = result.roll_no

                if not norm['is_valid']:
                    err_msg = f"অকার্যকর মোবাইল নম্বর ({target_phone})"
                    result.sms_status = 'failed'
                    result.sms_error = err_msg
                    result.save(update_fields=['sms_status', 'sms_error'])

                    SmsLog.objects.create(
                        exam=result.exam,
                        result=result,
                        recipient=target_phone,
                        roll_no=result.roll_no,
                        student_name=result.student_name,
                        message_text='(বার্তা পাঠানো যায়নি)',
                        status='failed',
                        error_message=err_msg
                    )

                    failed_local += 1
                    with _LOCK:
                        camp['failed_count'] = failed_local
                        camp['logs'].append({
                            'time': datetime.now().strftime('%I:%M:%S %p'),
                            'type': 'danger',
                            'msg': f"রোল {result.roll_no} ({target_phone}): {err_msg}"
                        })
                    continue

                # Render SMS
                rendered_msg = render_sms_template(template_val, result.exam, result, institute)

                # Send via modem
                sms_res = send_sms_via_modem(
                    port=port_val,
                    baudrate=baud_val,
                    recipient=norm['international'],
                    text=rendered_msg
                )

                time_now_str = datetime.now().strftime('%I:%M:%S %p')
                if sms_res.get('success'):
                    sent_local += 1
                    result.phone_number = norm['local']
                    result.sms_status = 'sent'
                    result.sms_sent_at = timezone.now()
                    result.sms_error = None
                    result.save(update_fields=['phone_number', 'sms_status', 'sms_sent_at', 'sms_error'])

                    SmsLog.objects.create(
                        exam=result.exam,
                        result=result,
                        recipient=norm['local'],
                        roll_no=result.roll_no,
                        student_name=result.student_name,
                        message_text=rendered_msg,
                        status='sent',
                        sent_at=timezone.now()
                    )

                    with _LOCK:
                        camp['sent_count'] = sent_local
                        camp['logs'].append({
                            'time': time_now_str,
                            'type': 'success',
                            'msg': f"রোল {result.roll_no} ({norm['local']}): সফলভাবে প্রেরিত"
                        })
                else:
                    failed_local += 1
                    err_msg = sms_res.get('error', 'মোডেম যোগাযোগ ত্রুটি')
                    result.phone_number = norm['local']
                    result.sms_status = 'failed'
                    result.sms_error = err_msg
                    result.save(update_fields=['phone_number', 'sms_status', 'sms_error'])

                    SmsLog.objects.create(
                        exam=result.exam,
                        result=result,
                        recipient=norm['local'],
                        roll_no=result.roll_no,
                        student_name=result.student_name,
                        message_text=rendered_msg,
                        status='failed',
                        error_message=err_msg
                    )

                    with _LOCK:
                        camp['failed_count'] = failed_local
                        camp['logs'].append({
                            'time': time_now_str,
                            'type': 'danger',
                            'msg': f"রোল {result.roll_no} ({norm['local']}): ব্যর্থ ({err_msg})"
                        })

                # Modem sleep delay between messages
                time.sleep(delay_val)

            except Exception as e:
                logger.exception(f"[SMS Campaign] Error processing result {res_id}: {e}")
                failed_local += 1
                with _LOCK:
                    camp['failed_count'] = failed_local
                    camp['logs'].append({
                        'time': datetime.now().strftime('%I:%M:%S %p'),
                        'type': 'danger',
                        'msg': f"আইটেম {res_id} প্রসেসিং ত্রুটি: {str(e)}"
                    })

        # Mark campaign completed
        with _LOCK:
            camp = _CAMPAIGNS.get(exam_id_val)
            if camp:
                camp['is_running'] = False
                camp['finished_at'] = datetime.now().strftime('%d %b %Y, %I:%M:%S %p')
                status_text = 'স্থগিত' if camp.get('stop_requested') else 'সম্পূর্ণ'
                camp['logs'].append({
                    'time': datetime.now().strftime('%I:%M:%S %p'),
                    'type': 'info',
                    'msg': f"এসএমএস প্রেরণ {status_text}! সফল: {camp['sent_count']}টি, ব্যর্থ: {camp['failed_count']}টি।"
                })

        # Close thread database connection
        connection.close()

    thread = threading.Thread(
        target=_worker,
        args=(
            exam.id,
            target_results,
            modem_settings.com_port,
            modem_settings.baud_rate,
            modem_settings.delay_seconds,
            modem_settings.sms_template
        ),
        daemon=True
    )
    thread.start()

    return True, None
