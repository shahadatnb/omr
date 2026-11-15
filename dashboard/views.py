import os
import re
import time
import json
import logging
from pathlib import Path
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.conf import settings
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.db.models import Q

from .models import BatchJob, ScanResult, StandaloneExam, StandaloneResult, InstituteSettings, ModemSettings, SmsLog
from .sms_worker import start_exam_sms_campaign, stop_exam_sms_campaign, get_campaign_status
from omr_engine.sheet_processor import process_sheet
from omr_engine.scorer import calculate_score
from omr_engine.modem_sms import (
    list_com_ports,
    normalize_bd_phone,
    test_modem,
    send_sms_via_modem,
    render_sms_template,
)
from integration.laravel_client import LaravelOmrClient

logger = logging.getLogger(__name__)

# In-memory answer key cache to minimize redundant Laravel calls per set
_ANSWER_KEY_CACHE = {}
_STUDENT_ROSTER_CACHE = {}
_TEMPLATE_DATA_CACHE = {}


def _get_template_data(template_path):
    """Loads and caches template JSON configuration."""
    if not template_path:
        return {}
    p = str(template_path)
    if p in _TEMPLATE_DATA_CACHE:
        return _TEMPLATE_DATA_CACHE[p]
    try:
        with open(p, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
            _TEMPLATE_DATA_CACHE[p] = data
            return data
    except Exception as e:
        logger.warning(f"Failed to read template JSON {p}: {e}")
        return {}


def _get_cached_answer_key(client, class_test_id, exam_set):
    set_clean = (exam_set or 'A').upper()
    cache_key = f"{class_test_id}_{set_clean}"
    if cache_key in _ANSWER_KEY_CACHE:
        return _ANSWER_KEY_CACHE[cache_key]
    try:
        ak = client.get_answer_key(class_test_id, set_clean)
        if ak and 'answers' in ak:
            _ANSWER_KEY_CACHE[cache_key] = ak
            return ak
    except Exception as e:
        logger.warning(f"Failed to fetch answer key for Set {set_clean}: {e}")
    return None


def _get_cached_student_roster(client, class_test_id):
    """
    Pre-fetch and cache the full student roster for a class test in memory.
    Indexes students by numeric reg_no and numeric class_role for instant O(1) matching.
    """
    if class_test_id in _STUDENT_ROSTER_CACHE:
        return _STUDENT_ROSTER_CACHE[class_test_id]

    roster = {}
    try:
        info = client.get_class_test_info(class_test_id)
        students = info.get('students', [])
        for s in students:
            reg_no = str(s.get('reg_no') or '')
            digits_reg = ''.join(ch for ch in reg_no if ch.isdigit())
            if digits_reg:
                roster[digits_reg] = s
                roster[digits_reg.lstrip('0')] = s

            role = str(s.get('class_role') or '')
            digits_role = ''.join(ch for ch in role if ch.isdigit())
            if digits_role:
                roster[digits_role] = s
                roster[digits_role.lstrip('0')] = s

        _STUDENT_ROSTER_CACHE[class_test_id] = roster
        logger.info(f"Loaded {len(students)} students into roster cache for Class Test #{class_test_id}")
    except Exception as e:
        logger.warning(f"Failed to prefetch student roster for test {class_test_id}: {e}")

    return roster


def get_available_templates():
    """
    Returns list of available templates in settings.OMR_TEMPLATES_DIR.
    """
    templates = []
    templates_dir = Path(settings.OMR_TEMPLATES_DIR)
    if templates_dir.exists():
        for f in sorted(templates_dir.glob('*.json')):
            try:
                with open(f, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                    name = data.get('name', f.stem)
            except Exception:
                name = f.stem
            templates.append({
                'filename': f.name,
                'name': name
            })
    return templates


def get_template_path(template_name=None, default='admission_challenge_100q.json'):
    """
    Safely resolves the Path to a template JSON file.
    """
    templates_dir = Path(settings.OMR_TEMPLATES_DIR)
    if template_name:
        safe_name = os.path.basename(template_name)
        candidate = templates_dir / safe_name
        if candidate.exists():
            return candidate
    candidate = templates_dir / default
    if candidate.exists():
        return candidate
    return templates_dir / 'admission_challenge_100q.json'


def process_single_scan_helper(scan, fpath, batch, client=None, template_path=None):
    """Process a single ScanResult, match student, calculate marks, and update stats."""
    if client is None:
        client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)
    if template_path is None:
        template_path = get_template_path(batch.template_name, default='admission_challenge_100q.json')

    try:
        res = process_sheet(fpath, template_path, debug=False)
        if res.get('success'):
            scan.student_numeric_id = res.get('student_numeric_id')
            scan.exam_set = res.get('exam_set')
            scan.answers_json = res.get('answers', {})

            # Calculate score if set has an answer key
            if scan.exam_set:
                ak = _get_cached_answer_key(client, batch.class_test_id, scan.exam_set)
                if ak:
                    tpl_data = _get_template_data(template_path)
                    score_res = calculate_score(
                        scan.answers_json,
                        ak['answers'],
                        correct_mark=ak.get('correct_mark', 1.0),
                        wrong_mark=ak.get('wrong_mark', -0.25),
                        subjects=tpl_data.get('subjects')
                    )
                    scan.marks = score_res['marks']
                    scan.subject_marks = score_res.get('subject_marks')
                    scan.correct_count = score_res['correct']
                    scan.wrong_count = score_res['wrong']
                    scan.unattempted_count = score_res['unattempted']

            # Duplicate Roll Protection (Option A: Reject & Alert)
            if scan.student_numeric_id and any(c.isdigit() for c in str(scan.student_numeric_id)):
                existing_scan = ScanResult.objects.filter(
                    batch=batch,
                    student_numeric_id=scan.student_numeric_id
                ).exclude(id=scan.id).first()
                if existing_scan:
                    scan.is_matched = False
                    scan.status = 'error'
                    scan.error_message = f"ডুপ্লিকেট ওএমআর শিট! রোল {scan.student_numeric_id} ইতিমধ্যে স্ক্যান করা হয়েছে ({existing_scan.original_filename})। ডুপ্লিকেট হিসেবে বাতিল করা হলো।"
                    scan.save()
                    batch.update_stats()
                    return scan

            # Match student with Laravel (in-memory fast lookup first, fallback to API)
            if scan.student_numeric_id:
                roster = _get_cached_student_roster(client, batch.class_test_id)
                num_id = str(scan.student_numeric_id).strip()
                student_data = roster.get(num_id) or roster.get(num_id.lstrip('0'))

                # Fallback to single live API lookup if student not in pre-fetched roster
                if not student_data:
                    try:
                        student_data = client.find_student(batch.class_test_id, scan.student_numeric_id)
                        if student_data and 'item_id' in student_data:
                            roster[num_id] = student_data
                    except Exception as e:
                        logger.warning(f"Fallback student lookup error for {num_id}: {e}")

                if student_data and 'item_id' in student_data:
                    scan.item_id = student_data['item_id']
                    scan.student_id = student_data['student_id']
                    scan.student_name = student_data.get('name')
                    scan.student_reg_no = student_data.get('reg_no')
                    scan.is_matched = True
                    scan.status = 'matched'
                    scan.error_message = None
                else:
                    scan.is_matched = False
                    scan.status = 'unmatched'
                    scan.error_message = f"রোল {scan.student_numeric_id} রস্টারে পাওয়া যায়নি"
            else:
                scan.is_matched = False
                scan.status = 'unmatched'
                scan.error_message = "শিটে রোলের বাবল পড়া যায়নি"
        else:
            scan.status = 'error'
            scan.error_message = res.get('error', 'OMR processing failed')

    except Exception as ex:
        logger.exception(f"Error processing {fpath}: {ex}")
        scan.status = 'error'
        scan.error_message = str(ex)

    scan.save()
    batch.update_stats()
    return scan


def batch_list(request):
    """List all OMR batch processing jobs."""
    batches = BatchJob.objects.all()
    return render(request, 'dashboard/batch_list.html', {'batches': batches})


def batch_upload(request):
    """
    Batch upload page:
    Loads existing batches so the user can either create a new batch
    or append files into an existing batch. Also loads available OMR templates.
    """
    existing_batches = BatchJob.objects.all().order_by('-id')[:50]
    available_templates = get_available_templates()
    return render(request, 'dashboard/batch_upload.html', {
        'existing_batches': existing_batches,
        'available_templates': available_templates,
    })


@require_POST
def batch_create_ajax(request):
    """
    AJAX endpoint to either initialize a new batch or verify an existing batch.
    """
    mode = request.POST.get('mode', 'new')
    client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)

    if mode == 'existing':
        batch_id = request.POST.get('batch_id')
        if not batch_id:
            return JsonResponse({'success': False, 'error': 'ব্যাচ নির্বাচন করুন।'}, status=400)
        batch = get_object_or_404(BatchJob, id=batch_id)
        return JsonResponse({
            'success': True,
            'batch_id': batch.id,
            'exam_title': batch.exam_title or f"Class Test #{batch.class_test_id}",
            'class_test_id': batch.class_test_id,
            'existing_count': batch.total_files,
            'mode': 'existing'
        })
    else:
        class_test_id_str = request.POST.get('class_test_id', '').strip()
        if not class_test_id_str:
            return JsonResponse({'success': False, 'error': 'Class Test ID প্রয়োজন।'}, status=400)
        try:
            class_test_id = int(class_test_id_str)
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Class Test ID একটি পূর্ণসংখ্যা হতে হবে।'}, status=400)

        template_name = request.POST.get('template_name', 'admission_challenge_100q.json').strip() or 'admission_challenge_100q.json'

        exam_title = f"Class Test #{class_test_id}"
        try:
            info = client.get_class_test_info(class_test_id)
            if 'ExamNo' in info:
                course_name = info.get('course', {}).get('CourseTitle', '')
                exam_title = f"{course_name} (Exam #{info['ExamNo']})"
        except Exception as e:
            logger.warning(f"Could not fetch class test info: {e}")

        batch = BatchJob.objects.create(
            class_test_id=class_test_id,
            exam_title=exam_title,
            template_name=template_name
        )
        return JsonResponse({
            'success': True,
            'batch_id': batch.id,
            'exam_title': batch.exam_title,
            'class_test_id': batch.class_test_id,
            'existing_count': 0,
            'mode': 'new'
        })


@require_POST
def upload_single_ajax(request, batch_id):
    """
    AJAX endpoint to upload and process a single OMR sheet into an existing batch.
    Returns real-time progress, extracted roll, set, student info, marks, and batch stats.
    """
    batch = get_object_or_404(BatchJob, id=batch_id)

    if 'file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file received'}, status=400)

    f = request.FILES['file']
    valid_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in valid_exts:
        return JsonResponse({'success': False, 'error': f'Invalid file type: {ext}'}, status=400)

    scan = ScanResult.objects.create(
        batch=batch,
        image_file=f,
        original_filename=f.name,
        status='pending'
    )

    process_single_scan_helper(scan, scan.image_file.path, batch)

    return JsonResponse({
        'success': True,
        'scan_id': scan.id,
        'filename': scan.original_filename,
        'student_numeric_id': scan.student_numeric_id,
        'exam_set': scan.exam_set,
        'is_matched': scan.is_matched,
        'student_name': scan.student_name,
        'student_reg_no': scan.student_reg_no,
        'marks': round(scan.marks, 2),
        'correct_count': scan.correct_count,
        'wrong_count': scan.wrong_count,
        'unattempted_count': scan.unattempted_count,
        'status': scan.status,
        'error_message': scan.error_message,
        'batch_stats': {
            'total': batch.total_files,
            'processed': batch.processed_count,
            'matched': batch.matched_count,
            'unmatched': batch.unmatched_count,
        }
    })


@require_POST
def scan_folder_ajax(request, batch_id):
    """
    AJAX endpoint for Option 2 (server local folder path).
    Scans the folder, creates pending ScanResult objects, and returns their IDs.
    The client will then call process_item_ajax for each item sequentially.
    """
    batch = get_object_or_404(BatchJob, id=batch_id)
    server_folder = request.POST.get('server_folder', '').strip()

    if not server_folder or not os.path.exists(server_folder):
        return JsonResponse({'success': False, 'error': 'ফোল্ডার পাওয়া যায়নি। পাথ সঠিক কিনা যাচাই করুন।'}, status=400)

    valid_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
    items = []

    for root, _, filenames in os.walk(server_folder):
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext in valid_exts:
                full_path = os.path.join(root, fname)
                scan = ScanResult.objects.create(
                    batch=batch,
                    original_filename=fname,
                    status='pending'
                )
                items.append({
                    'scan_id': scan.id,
                    'filename': fname,
                    'fpath': full_path,
                })

    batch.update_stats()
    return JsonResponse({
        'success': True,
        'total_queued': len(items),
        'items': items
    })


@require_POST
def process_item_ajax(request, batch_id, scan_id):
    """
    AJAX endpoint to process a pending ScanResult from server folder.
    """
    batch = get_object_or_404(BatchJob, id=batch_id)
    scan = get_object_or_404(ScanResult, id=scan_id, batch=batch)
    fpath = request.POST.get('fpath')

    if not fpath and scan.image_file:
        fpath = scan.image_file.path

    if not fpath or not os.path.exists(fpath):
        scan.status = 'error'
        scan.error_message = 'ফাইল খুঁজে পাওয়া যায়নি।'
        scan.save()
        batch.update_stats()
        return JsonResponse({'success': False, 'error': 'File not found'}, status=404)

    process_single_scan_helper(scan, fpath, batch)

    return JsonResponse({
        'success': True,
        'scan_id': scan.id,
        'filename': scan.original_filename,
        'student_numeric_id': scan.student_numeric_id,
        'exam_set': scan.exam_set,
        'is_matched': scan.is_matched,
        'student_name': scan.student_name,
        'student_reg_no': scan.student_reg_no,
        'marks': round(scan.marks, 2),
        'correct_count': scan.correct_count,
        'wrong_count': scan.wrong_count,
        'unattempted_count': scan.unattempted_count,
        'status': scan.status,
        'error_message': scan.error_message,
        'batch_stats': {
            'total': batch.total_files,
            'processed': batch.processed_count,
            'matched': batch.matched_count,
            'unmatched': batch.unmatched_count,
        }
    })


def batch_detail(request, batch_id):
    """View details of a batch job with matched and unmatched sheets."""
    batch = get_object_or_404(BatchJob, id=batch_id)
    batch.update_stats()
    filter_type = request.GET.get('filter', 'all')

    results = batch.results.all()
    if filter_type == 'matched':
        results = results.filter(is_matched=True)
    elif filter_type == 'unmatched':
        results = results.filter(is_matched=False)

    tpl_path = get_template_path(batch.template_name)
    tpl_data = _get_template_data(tpl_path)
    subjects = tpl_data.get('subjects', [])

    return render(request, 'dashboard/batch_detail.html', {
        'batch': batch,
        'results': results,
        'filter_type': filter_type,
        'subjects': subjects,
    })


def manual_edit(request, result_id):
    """
    Manual editing for unmatched or misread sheets.
    Allows teacher to:
    1. Select the correct student from the class roster dropdown
    2. Correct the Roll/Reg numeric ID or Exam Set (A/B/C/D)
    3. Recalculates marks immediately and marks as matched/manual!
    """
    result = get_object_or_404(ScanResult, id=result_id)
    batch = result.batch

    client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)

    enrolled_students = []
    try:
        info = client.get_class_test_info(batch.class_test_id)
        enrolled_students = info.get('students', [])
    except Exception as e:
        messages.warning(request, f"Could not load roster from Laravel: {e}")

    if request.method == 'POST':
        selected_item_id = request.POST.get('item_id')
        exam_set = request.POST.get('exam_set', result.exam_set or 'A').upper()
        custom_numeric_id = request.POST.get('student_numeric_id', '').strip()

        chosen_student = None
        if selected_item_id:
            try:
                selected_item_id = int(selected_item_id)
                for s in enrolled_students:
                    if s.get('item_id') == selected_item_id:
                        chosen_student = s
                        break
            except ValueError:
                pass

        if chosen_student:
            result.item_id = chosen_student['item_id']
            result.student_id = chosen_student['student_id']
            result.student_name = chosen_student['name']
            result.student_reg_no = chosen_student['reg_no']
            result.student_numeric_id = custom_numeric_id or ''.join(filter(str.isdigit, str(chosen_student['reg_no'])))
            result.is_matched = True
        elif result.is_matched and result.item_id:
            # Student was already matched; keep current student and allow updating numeric id if given
            if custom_numeric_id:
                result.student_numeric_id = custom_numeric_id
        else:
            messages.error(request, 'অনুগ্রহ করে তালিকা থেকে শিক্ষার্থী নির্বাচন করুন।')
            return render(request, 'dashboard/manual_edit.html', {
                'result': result,
                'batch': batch,
                'enrolled_students': enrolled_students,
            })

        result.exam_set = exam_set
        result.is_manually_edited = True
        result.status = 'manual'
        result.error_message = None

        # Recalculate score with the chosen set
        try:
            ak = _get_cached_answer_key(client, batch.class_test_id, exam_set)
            if ak and 'answers' in ak:
                tpl_path = get_template_path(batch.template_name)
                tpl_data = _get_template_data(tpl_path)
                score_res = calculate_score(
                    result.answers_json,
                    ak['answers'],
                    correct_mark=ak.get('correct_mark', 1.0),
                    wrong_mark=ak.get('wrong_mark', -0.25),
                    subjects=tpl_data.get('subjects')
                )
                result.marks = score_res['marks']
                result.subject_marks = score_res.get('subject_marks')
                result.correct_count = score_res['correct']
                result.wrong_count = score_res['wrong']
                result.unattempted_count = score_res['unattempted']
            else:
                messages.warning(request, f"Set {exam_set}-এর Answer Key লারাভেলে পাওয়া যায়নি।")
        except Exception as ex:
            logger.warning(f"Error recalculating score during manual edit: {ex}")

        result.save()
        batch.update_stats()

        messages.success(request, f"ওএমআর শিট সফলভাবে হালনাগাদ করা হয়েছে (সেট: {result.exam_set})। প্রাপ্ত নম্বর: {result.marks}")
        return redirect('dashboard:batch_detail', batch_id=batch.id)

    return render(request, 'dashboard/manual_edit.html', {
        'result': result,
        'batch': batch,
        'enrolled_students': enrolled_students,
    })


@require_POST
def scan_result_change_set_ajax(request, result_id):
    """
    AJAX endpoint to quickly correct the exam set of a ScanResult (online/batch).
    Recalculates score immediately against the new set's answer key.
    """
    result = get_object_or_404(ScanResult, id=result_id)
    batch = result.batch
    new_set = request.POST.get('exam_set', '').strip().upper()

    if new_set not in ['A', 'B', 'C', 'D']:
        return JsonResponse({'success': False, 'error': 'অবৈধ সেট। শুধু A, B, C অথবা D নির্বাচন করুন।'}, status=400)

    result.exam_set = new_set
    result.is_manually_edited = True
    if result.status == 'matched':
        result.status = 'manual'

    # Recalculate score
    client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)
    ak = _get_cached_answer_key(client, batch.class_test_id, new_set)
    if ak and 'answers' in ak:
        tpl_path = get_template_path(batch.template_name)
        tpl_data = _get_template_data(tpl_path)
        score_res = calculate_score(
            result.answers_json,
            ak['answers'],
            correct_mark=ak.get('correct_mark', 1.0),
            wrong_mark=ak.get('wrong_mark', -0.25),
            subjects=tpl_data.get('subjects')
        )
        result.marks = score_res['marks']
        result.subject_marks = score_res.get('subject_marks')
        result.correct_count = score_res['correct']
        result.wrong_count = score_res['wrong']
        result.unattempted_count = score_res['unattempted']
        result.error_message = None
    else:
        result.error_message = f"Set {new_set}-এর কোনো Answer Key লারাভেলে পাওয়া যায়নি।"

    result.save()
    batch.update_stats()

    return JsonResponse({
        'success': True,
        'id': result.id,
        'exam_set': result.exam_set,
        'marks': round(result.marks, 2),
        'correct_count': result.correct_count,
        'wrong_count': result.wrong_count,
        'unattempted_count': result.unattempted_count,
        'status': result.status,
        'error_message': result.error_message,
        'message': f"সেট পরিবর্তন করে Set {new_set} করা হয়েছে এবং নম্বর পুনর্মূল্যায়ন হয়েছে।"
    })


@require_POST
def batch_regenerate_results_ajax(request, batch_id):
    """
    AJAX endpoint to recalculate scores for all sheets in an online batch.
    Pulls fresh answer keys from Laravel for all sets (A, B, C, D) and recalculates marks.
    """
    batch = get_object_or_404(BatchJob, id=batch_id)
    client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)

    # Invalidate cached answer keys
    _ANSWER_KEY_CACHE.clear()

    tpl_path = get_template_path(batch.template_name)
    tpl_data = _get_template_data(tpl_path)
    subjects = tpl_data.get('subjects')

    keys = {}
    for s in ['A', 'B', 'C', 'D']:
        ak = _get_cached_answer_key(client, batch.class_test_id, s)
        if ak and 'answers' in ak:
            keys[s] = ak

    rescored_count = 0
    missing_key_sets = set()

    for res in batch.results.all():
        s = res.exam_set or 'A'
        ak = keys.get(s)
        if ak and 'answers' in ak:
            score_res = calculate_score(
                res.answers_json,
                ak['answers'],
                correct_mark=ak.get('correct_mark', 1.0),
                wrong_mark=ak.get('wrong_mark', -0.25),
                subjects=subjects
            )
            res.marks = score_res['marks']
            res.subject_marks = score_res.get('subject_marks')
            res.correct_count = score_res['correct']
            res.wrong_count = score_res['wrong']
            res.unattempted_count = score_res['unattempted']
            res.error_message = None
            res.save()
            rescored_count += 1
        else:
            missing_key_sets.add(s)

    batch.update_stats()

    msg = f"মোট {rescored_count}টি শিটের ফলাফল সফলভাবে রিজেনারেট ও পুনর্মূল্যায়ন করা হয়েছে!"
    if missing_key_sets:
        msg += f" (সতর্কতা: Set {', '.join(sorted(missing_key_sets))}-এর Answer Key লারাভেলে পাওয়া যায়নি।)"

    return JsonResponse({
        'success': True,
        'message': msg,
        'rescored_count': rescored_count,
        'total_count': batch.results.count()
    })


def sync_to_laravel(request, batch_id):
    """
    Push all matched & manually resolved marks to Laravel /api/omr/batch.
    """
    batch = get_object_or_404(BatchJob, id=batch_id)
    matched_results = batch.results.filter(is_matched=True, item_id__isnull=False)

    if not matched_results.exists():
        messages.error(request, 'No matched results found to sync.')
        return redirect('dashboard:batch_detail', batch_id=batch.id)

    payload = []
    for r in matched_results:
        item_data = {
            'item_id': r.item_id,
            'marks': r.marks,
            'correct_answer': r.correct_count,
            'wrong_answer': r.wrong_count,
            'is_absend': 0,
        }
        if r.subject_marks:
            item_data['subject_marks'] = r.subject_marks
        payload.append(item_data)

    client = LaravelOmrClient(base_url=settings.LARAVEL_API_URL)
    try:
        res = client.batch_update_results(payload)
        batch.synced_to_laravel = True
        batch.synced_at = timezone.now()
        batch.save()
        messages.success(request, f"Successfully synced {res.get('updated', len(payload))} student marks to Laravel!")
    except Exception as e:
        logger.exception("Failed to sync to Laravel")
        messages.error(request, f"Failed to sync with Laravel API: {e}")

    return redirect('dashboard:batch_detail', batch_id=batch.id)


@require_POST
def batch_delete(request, batch_id):
    """Delete an entire batch and its scan results."""
    batch = get_object_or_404(BatchJob, id=batch_id)
    batch.delete()
    messages.success(request, f"Batch #{batch_id} মুছে ফেলা হয়েছে।")
    return redirect('dashboard:batch_list')


@require_POST
def scan_result_delete(request, result_id):
    """Delete an individual scan result from a batch."""
    result = get_object_or_404(ScanResult, id=result_id)
    batch = result.batch
    result.delete()
    batch.update_stats()
    messages.success(request, "স্ক্যান রেকর্ড মুছে ফেলা হয়েছে।")
    return redirect('dashboard:batch_detail', batch_id=batch.id)


def template_editor(request):
    """
    Visual Template Editor (HTML5 Canvas).
    Supports multiple templates:
    - Switch templates via ?template=<filename>
    - Save modifications to selected template
    - 'Save As' to create a new template copy
    """
    available_templates = get_available_templates()
    selected_filename = request.GET.get('template') or request.POST.get('template_filename') or 'admission_challenge_100q.json'
    selected_filename = os.path.basename(selected_filename)
    template_path = get_template_path(selected_filename, default='admission_challenge_100q.json')

    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        if action == 'wizard_create':
            try:
                w_title = request.POST.get('wizard_title', '').strip() or 'New OMR Template'
                w_filename = request.POST.get('wizard_filename', '').strip() or 'custom_template.json'
                if not w_filename.endswith('.json'):
                    w_filename += '.json'
                w_filename = os.path.basename(w_filename)

                w_orientation = request.POST.get('wizard_orientation', 'portrait')
                w_width = int(request.POST.get('wizard_width') or (794 if w_orientation == 'portrait' else 1123))
                w_height = int(request.POST.get('wizard_height') or (1123 if w_orientation == 'portrait' else 794))
                w_total_q = int(request.POST.get('wizard_total_q') or 100)
                w_cols = int(request.POST.get('wizard_cols') or 4)
                w_roll_digits = int(request.POST.get('wizard_roll_digits') or 6)
                w_sets = int(request.POST.get('wizard_sets') or 4)

                stem = os.path.splitext(w_filename)[0]
                sample_rel_url = None

                # Process sample image if provided
                if 'wizard_sample_image' in request.FILES:
                    f = request.FILES['wizard_sample_image']
                    import cv2
                    import numpy as np
                    from omr_engine.perspective import find_corner_markers, warp_sheet
                    file_bytes = np.frombuffer(f.read(), np.uint8)
                    img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
                    if img is not None:
                        corners = find_corner_markers(img)
                        if corners:
                            warped = warp_sheet(img, corners, output_size=(w_width, w_height))
                        else:
                            warped = cv2.resize(img, (w_width, w_height))
                        out_dir = settings.BASE_DIR / 'media' / 'templates'
                        out_dir.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(out_dir / f"{stem}_sample.jpg"), warped)
                        sample_rel_url = f"/media/templates/{stem}_sample.jpg"

                # Calculate evenly distributed columns
                q_per_col = (w_total_q + w_cols - 1) // w_cols
                answer_cols = []
                margin_x = int(w_width * 0.08)
                avail_w = w_width - 2 * margin_x
                col_gap = int(avail_w * 0.04) if w_cols > 1 else 0
                col_w = int((avail_w - (w_cols - 1) * col_gap) / max(w_cols, 1))
                top_y = int(w_height * 0.42) if w_orientation == 'portrait' else int(w_height * 0.45)
                col_h = int(w_height * 0.53) if w_orientation == 'portrait' else int(w_height * 0.48)

                q_curr = 1
                for i in range(w_cols):
                    q_start = q_curr
                    q_end = min(q_start + q_per_col - 1, w_total_q)
                    q_curr = q_end + 1
                    col_x = margin_x + i * (col_w + col_gap)
                    answer_cols.append({
                        "column_index": i + 1,
                        "subject": f"কলাম {i+1}",
                        "q_start": q_start,
                        "q_end": q_end,
                        "x": col_x,
                        "y": top_y,
                        "w": col_w,
                        "h": col_h
                    })

                new_template_data = {
                    "name": w_title,
                    "version": "1.0",
                    "dimensions": {"width": w_width, "height": w_height},
                    "orientation": w_orientation,
                    "corner_markers": {"min_area": 100, "max_area": 10000, "aspect_tolerance": 0.4},
                    "id_grid": {
                        "x": int(w_width * 0.68),
                        "y": int(w_height * 0.07),
                        "w": int(w_width * 0.24),
                        "h": int(w_height * 0.28),
                        "columns": w_roll_digits,
                        "rows": 10
                    },
                    "set_region": {
                        "x": int(w_width * 0.60),
                        "y": int(w_height * 0.07),
                        "w": int(w_width * 0.05),
                        "h": int(w_height * 0.13),
                        "rows": w_sets if w_sets > 0 else 4,
                        "columns": 1
                    },
                    "answer_columns": answer_cols
                }
                if sample_rel_url:
                    new_template_data["sample_image"] = sample_rel_url

                save_path = settings.OMR_TEMPLATES_DIR / w_filename
                with open(save_path, 'w', encoding='utf-8') as f:
                    json.dump(new_template_data, f, indent=2, ensure_ascii=False)

                messages.success(request, f'নতুন টেমপ্লেট "{w_title}" ({w_filename}) সফলভাবে তৈরি হয়েছে!')
                return redirect(f"{request.path}?template={w_filename}")
            except Exception as e:
                logger.exception("Failed wizard creation")
                messages.error(request, f'টেমপ্লেট তৈরি করতে ব্যর্থ: {e}')
                return redirect(f"{request.path}?template={selected_filename}")

        try:
            new_data_str = request.POST.get('template_json')
            if not new_data_str:
                messages.error(request, 'টেমপ্লেট ডাটা পাওয়া যায়নি।')
                return redirect(f"{request.path}?template={selected_filename}")

            parsed = json.loads(new_data_str)

            if action == 'save_as':
                new_title = request.POST.get('new_title', '').strip()
                new_filename = request.POST.get('new_filename', '').strip()
                if not new_filename:
                    messages.error(request, 'নতুন ফাইলের নাম আবশ্যক।')
                    return redirect(f"{request.path}?template={selected_filename}")

                if not new_filename.endswith('.json'):
                    new_filename += '.json'
                new_filename = os.path.basename(new_filename)

                if new_title:
                    parsed['name'] = new_title

                save_path = settings.OMR_TEMPLATES_DIR / new_filename
                with open(save_path, 'w', encoding='utf-8') as f:
                    json.dump(parsed, f, indent=2, ensure_ascii=False)

                messages.success(request, f'নতুন টেমপ্লেট "{new_filename}" সফলভাবে তৈরি ও সংরক্ষিত হয়েছে!')
                return redirect(f"{request.path}?template={new_filename}")
            else:
                # Normal save to current file
                with open(template_path, 'w', encoding='utf-8') as f:
                    json.dump(parsed, f, indent=2, ensure_ascii=False)

                messages.success(request, f'টেমপ্লেট "{template_path.name}" সফলভাবে সংরক্ষিত হয়েছে!')
                return redirect(f"{request.path}?template={template_path.name}")

        except Exception as e:
            logger.exception("Failed to save template")
            messages.error(request, f'টেমপ্লেট সেভ করতে ব্যর্থ: {e}')
            return redirect(f"{request.path}?template={selected_filename}")

    # Load existing template
    template_data = {}
    if template_path.exists():
        with open(template_path, 'r', encoding='utf-8') as f:
            template_data = json.load(f)

    # Determine template-specific sample image url
    sample_image_url = None
    if 'sample_image' in template_data and template_data['sample_image']:
        cand_path = settings.BASE_DIR / template_data['sample_image'].lstrip('/')
        if cand_path.exists():
            sample_image_url = template_data['sample_image']

    stem = os.path.splitext(template_path.name)[0]
    if not sample_image_url:
        stem_sample = settings.BASE_DIR / 'media' / 'templates' / f"{stem}_sample.jpg"
        if stem_sample.exists():
            sample_image_url = f"/media/templates/{stem}_sample.jpg"

    if not sample_image_url:
        sample_image_url = '/media/templates/sample_warped.jpg'

    return render(request, 'dashboard/template_editor.html', {
        'available_templates': available_templates,
        'current_template_filename': template_path.name,
        'template_data': template_data,
        'template_json_str': json.dumps(template_data, indent=2),
        'sample_image_url': sample_image_url,
    })


@require_POST
def template_upload_sample_ajax(request):
    """
    Upload and warp a reference/sample sheet for the currently edited template.
    Uses corner marker detection and perspective warp to align it to 794x1123,
    then updates the template configuration so the editor uses this sheet as background.
    """
    if 'sample_image' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'কোনো ইমেজ ফাইল পাওয়া যায়নি।'}, status=400)

    f = request.FILES['sample_image']
    template_filename = request.POST.get('template_filename', 'admission_challenge_100q.json')
    template_filename = os.path.basename(template_filename)
    stem = os.path.splitext(template_filename)[0]

    try:
        import time
        import cv2
        import numpy as np
        from omr_engine.perspective import find_corner_markers, warp_sheet

        # Read uploaded image into memory
        file_bytes = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        if img is None:
            return JsonResponse({'success': False, 'error': 'ছবিটি পড়া সম্ভব হয়নি। সঠিক ইমেজ ফাইল দিন।'}, status=400)

        # Determine target dimensions (portrait 794x1123, landscape 1123x794, or custom)
        target_w = 794
        target_h = 1123
        try:
            if request.POST.get('width') and request.POST.get('height'):
                target_w = int(request.POST['width'])
                target_h = int(request.POST['height'])
            else:
                template_file_path = get_template_path(template_filename)
                if template_file_path.exists():
                    with open(template_file_path, 'r', encoding='utf-8') as tf:
                        t_data = json.load(tf)
                    dims = t_data.get('dimensions', {})
                    if dims.get('width') and dims.get('height'):
                        target_w = int(dims['width'])
                        target_h = int(dims['height'])
        except Exception:
            pass

        # Detect corners and warp
        corners = find_corner_markers(img)
        corners_found = False
        if corners:
            warped = warp_sheet(img, corners, output_size=(target_w, target_h))
            corners_found = True
        else:
            warped = cv2.resize(img, (target_w, target_h))

        # Save warped reference image
        out_filename = f"{stem}_sample.jpg"
        out_dir = settings.BASE_DIR / 'media' / 'templates'
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / out_filename
        cv2.imwrite(str(out_path), warped)

        # Update active template JSON
        template_file_path = get_template_path(template_filename)
        sample_rel_url = f"/media/templates/{out_filename}"
        if template_file_path.exists():
            try:
                with open(template_file_path, 'r', encoding='utf-8') as tf:
                    t_data = json.load(tf)
                t_data['sample_image'] = sample_rel_url
                with open(template_file_path, 'w', encoding='utf-8') as tf:
                    json.dump(t_data, tf, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Could not update template JSON with sample_image: {e}")

        ts = int(time.time())
        msg = 'নমুনা শিট সফলভাবে লোড ও কোণা সোজা (Warp) করা হয়েছে।' if corners_found else 'নমুনা শিট লোড হয়েছে (কোণার ৪টি চিহ্ন না পাওয়ায় সরাসরি রিসাইজ করা হয়েছে)।'
        return JsonResponse({
            'success': True,
            'sample_url': f"{sample_rel_url}?v={ts}",
            'corners_found': corners_found,
            'message': msg
        })
    except Exception as e:
        logger.exception("Sample upload error")
        return JsonResponse({'success': False, 'error': f'ত্রুটি: {e}'}, status=500)


@require_POST
def template_rotate_sample_ajax(request):
    """
    Rotate the current template sample image by 90 degrees clockwise.
    Persists the rotated image so template editor displays it correctly.
    """
    template_filename = request.POST.get('template_filename', 'admission_challenge_100q.json')
    template_filename = os.path.basename(template_filename)
    stem = os.path.splitext(template_filename)[0]
    out_dir = settings.BASE_DIR / 'media' / 'templates'
    sample_path = out_dir / f"{stem}_sample.jpg"

    if not sample_path.exists():
        # Fallback to general sample
        sample_path = out_dir / 'sample_warped.jpg'

    if not sample_path.exists():
        return JsonResponse({'success': False, 'error': 'নমুনা শিট ফাইলটি পাওয়া যায়নি।'}, status=404)

    try:
        import cv2
        import time
        img = cv2.imread(str(sample_path))
        if img is None:
            return JsonResponse({'success': False, 'error': 'নমুনা শিট পড়তে ব্যর্থ হয়েছে।'}, status=400)

        rotated = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)

        target_path = out_dir / f"{stem}_sample.jpg"
        cv2.imwrite(str(target_path), rotated)

        sample_rel_url = f"/media/templates/{stem}_sample.jpg"
        ts = int(time.time())

        return JsonResponse({
            'success': True,
            'sample_url': f"{sample_rel_url}?v={ts}",
            'width': rotated.shape[1],
            'height': rotated.shape[0],
            'message': 'নমুনা শিট সফলভাবে ৯০° ঘোরানো হয়েছে।'
        })
    except Exception as e:
        logger.exception("Sample rotate error")
        return JsonResponse({'success': False, 'error': f'ত্রুটি: {e}'}, status=500)


@require_POST
def template_test_ajax(request):
    """
    Test OMR recognition with live coordinates from the Template Editor
    against the active template's sample image.
    """
    import tempfile
    try:
        coords_str = request.POST.get('template_json')
        if not coords_str:
            return JsonResponse({'success': False, 'error': 'No template data provided'}, status=400)

        template_dict = json.loads(coords_str)
        template_filename = request.POST.get('template_filename') or 'admission_challenge_100q.json'
        stem = os.path.splitext(os.path.basename(template_filename))[0]

        # Determine sample image: template-specific first, then general sample
        sample_path = settings.BASE_DIR / 'media' / 'templates' / f"{stem}_sample.jpg"
        if not sample_path.exists() and 'sample_image' in template_dict and template_dict['sample_image']:
            candidate = settings.BASE_DIR / template_dict['sample_image'].lstrip('/')
            if candidate.exists():
                sample_path = candidate

        if not sample_path.exists():
            sample_path = settings.BASE_DIR / 'media' / 'templates' / 'sample_warped.jpg'
        if not sample_path.exists():
            sample_path = settings.BASE_DIR / 'warped_sample.jpg'

        if not sample_path.exists():
            return JsonResponse({'success': False, 'error': 'নমুনা শিটের ইমেজ পাওয়া যায়নি'}, status=404)

        # Write temp template JSON to test
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.json', encoding='utf-8') as tf:
            json.dump(template_dict, tf)
            temp_template_path = tf.name

        try:
            res = process_sheet(str(sample_path), temp_template_path, debug=True)
            # Pick first 15 answers for textual display, and return all answers for visual overlay
            all_answers = {str(k): v for k, v in res.get('answers', {}).items()}
            answers_sample = {str(k): res['answers'][k] for k in list(res['answers'].keys())[:15]} if res.get('answers') else {}
            filled_count = sum(1 for v in res.get('answers', {}).values() if v is not None)

            return JsonResponse({
                'success': res.get('success', False),
                'student_numeric_id': res.get('student_numeric_id'),
                'exam_set': res.get('exam_set'),
                'answered_count': filled_count,
                'total_questions': len(res.get('answers', {})),
                'sample_answers': answers_sample,
                'all_answers': all_answers,
                'error': res.get('error')
            })
        finally:
            if os.path.exists(temp_template_path):
                os.remove(temp_template_path)

    except Exception as e:
        logger.exception("Template test error")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_POST
def template_autodetect_ajax(request):
    """
    Intelligently detect table grid boundaries, answer columns, roll ID grid,
    and set box from the sheet image using OpenCV morphological operations.
    """
    template_filename = request.POST.get('template_filename') or 'admission_challenge_100q.json'
    stem = os.path.splitext(os.path.basename(template_filename))[0]

    import cv2
    import numpy as np

    img = None
    if 'sample_image' in request.FILES:
        f = request.FILES['sample_image']
        file_bytes = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        from omr_engine.perspective import find_corner_markers, warp_sheet
        target_w = int(request.POST.get('width') or 794)
        target_h = int(request.POST.get('height') or 1123)
        if img is not None:
            corners = find_corner_markers(img)
            if corners:
                img = warp_sheet(img, corners, output_size=(target_w, target_h))
            else:
                img = cv2.resize(img, (target_w, target_h))
    else:
        sample_path = settings.BASE_DIR / 'media' / 'templates' / f"{stem}_sample.jpg"
        if not sample_path.exists():
            sample_path = settings.BASE_DIR / 'media' / 'templates' / 'sample_warped.jpg'
        if sample_path.exists():
            img = cv2.imread(str(sample_path))

    if img is None:
        return JsonResponse({'success': False, 'error': 'নমুনা শিটের ইমেজ পাওয়া যায়নি।'}, status=404)

    try:
        H, W = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        bw = cv2.adaptiveThreshold(~gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)

        vert_size = max(H // 40, 10)
        vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vert_size))
        vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vert_kernel)

        horiz_size = max(W // 40, 10)
        horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_size, 1))
        horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, horiz_kernel)

        table = cv2.add(vert, horiz)
        contours, _ = cv2.findContours(table, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w > 0.95 * W and h > 0.95 * H:
                continue
            if w >= 25 and h >= 70:
                candidates.append({'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h)})

        # 1. Answer columns: tall boxes (h >= 0.35 * H) aligned at similar y level
        tall_candidates = [b for b in candidates if b['h'] >= 0.35 * H]

        groups = []
        for b in sorted(tall_candidates, key=lambda x: x['y']):
            placed = False
            for g in groups:
                if abs(g[0]['y'] - b['y']) < 60 and abs(g[0]['h'] - b['h']) < 80:
                    g.append(b)
                    placed = True
                    break
            if not placed:
                groups.append([b])

        columns = []
        if groups:
            groups.sort(key=lambda g: (-len(g), -g[0]['h']))
            raw_cols = sorted(groups[0], key=lambda b: b['x'])
            median_w = sorted([c['w'] for c in raw_cols])[len(raw_cols) // 2]
            for c in raw_cols:
                # If two adjacent columns merged into one wide contour, split them
                if c['w'] >= 1.7 * median_w:
                    half_w = c['w'] // 2
                    columns.append({'x': c['x'], 'y': c['y'], 'w': half_w, 'h': c['h']})
                    columns.append({'x': c['x'] + half_w, 'y': c['y'], 'w': c['w'] - half_w, 'h': c['h']})
                else:
                    columns.append(c)

        # 2. Roll candidates: boxes not in tall answer columns, w >= 70, h >= 100
        roll_candidates = [b for b in candidates if b not in tall_candidates and b['w'] >= 70 and b['h'] >= 100]
        roll_candidates.sort(key=lambda b: (b['y'], -b['w']))

        # 3. Set candidates: narrow box with aspect ratio >= 1.6, w <= 70, h >= 70
        set_candidates = [b for b in candidates if b not in tall_candidates and b['w'] <= 70 and b['h'] >= 70 and (b['h'] / max(b['w'], 1)) >= 1.6]
        set_candidates.sort(key=lambda b: (b['y'], b['x']))

        detected_roll = roll_candidates[0] if roll_candidates else None
        detected_set = set_candidates[0] if set_candidates else None

        if not columns and not detected_roll:
            return JsonResponse({
                'success': False,
                'error': 'স্বয়ংক্রিয়ভাবে গ্রিড শনাক্ত করা সম্ভব হয়নি। শিটে স্পষ্ট বর্ডার লাইন আছে কি না নিশ্চিত করুন অথবা ক্যানভাসে ড্র মোড ব্যবহার করুন।'
            })

        return JsonResponse({
            'success': True,
            'dimensions': {'width': W, 'height': H},
            'columns': columns,
            'roll': detected_roll,
            'set': detected_set,
            'total_columns': len(columns),
            'message': f'স্বয়ংক্রিয়ভাবে {len(columns)}টি কলাম' + (', ১টি রোল গ্রিড' if detected_roll else '') + (', ১টি সেট বক্স' if detected_set else '') + ' শনাক্ত হয়েছে!'
        })
    except Exception as e:
        logger.exception("Autodetect error")
        return JsonResponse({'success': False, 'error': f'ডিটেকশন ত্রুটি: {str(e)}'}, status=500)


@require_POST
def template_delete_ajax(request):
    """Safely delete a custom template (except default_100q.json)."""
    template_filename = request.POST.get('template_filename', '').strip()
    template_filename = os.path.basename(template_filename)
    if not template_filename or template_filename == 'default_100q.json':
        return JsonResponse({'success': False, 'error': 'ডিফল্ট টেমপ্লেট মুছে ফেলা যাবে না।'}, status=400)

    target_path = settings.OMR_TEMPLATES_DIR / template_filename
    if not target_path.exists():
        return JsonResponse({'success': False, 'error': 'টেমপ্লেট ফাইলটি পাওয়া যায়নি।'}, status=404)

    try:
        os.remove(target_path)
        stem = os.path.splitext(template_filename)[0]
        sample_path = settings.BASE_DIR / 'media' / 'templates' / f"{stem}_sample.jpg"
        if sample_path.exists():
            os.remove(sample_path)

        return JsonResponse({'success': True, 'message': f'"{template_filename}" সফলভাবে মুছে ফেলা হয়েছে।'})
    except Exception as e:
        logger.exception("Template delete error")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ==============================================================================
# STANDALONE / OFFLINE OMR EXAM SYSTEM (NO API DEPENDENCY)
# ==============================================================================

def standalone_list(request):
    """List all standalone offline exams."""
    exams = StandaloneExam.objects.all().order_by('-created_at')
    return render(request, 'dashboard/standalone/exam_list.html', {'exams': exams})


def standalone_create(request):
    """Create a new standalone exam with scoring rules, selected OMR template, and optional key copying from previous exam."""
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        exam_date = request.POST.get('exam_date')
        total_questions = int(request.POST.get('total_questions', 100))
        correct_mark = float(request.POST.get('correct_mark', 1.0))
        wrong_mark = float(request.POST.get('wrong_mark', -0.25))
        template_name = request.POST.get('template_name', 'offline_exam_100q.json').strip() or 'offline_exam_100q.json'
        copy_from_id = request.POST.get('copy_keys_from_exam_id')

        if not title:
            messages.error(request, 'পরীক্ষার নাম লিখুন।')
            return redirect('dashboard:standalone_create')

        exam = StandaloneExam.objects.create(
            title=title,
            exam_date=exam_date or timezone.now().date(),
            total_questions=total_questions,
            correct_mark=correct_mark,
            wrong_mark=wrong_mark,
            template_name=template_name
        )

        copied_msg = ""
        if copy_from_id:
            try:
                source_exam = StandaloneExam.objects.get(id=int(copy_from_id))
                exam.answers_set_a = source_exam.answers_set_a or {}
                exam.answers_set_b = source_exam.answers_set_b or {}
                exam.answers_set_c = source_exam.answers_set_c or {}
                exam.answers_set_d = source_exam.answers_set_d or {}
                exam.save()
                copied_msg = f' এবং "{source_exam.title}" থেকে Answer Key সফলভাবে কপি করা হয়েছে!'
            except (StandaloneExam.DoesNotExist, ValueError):
                pass

        messages.success(request, f'পরীক্ষা "{exam.title}" তৈরি হয়েছে{copied_msg}।')
        return redirect('dashboard:standalone_answer_key', exam_id=exam.id)

    available_templates = get_available_templates()
    previous_exams = StandaloneExam.objects.all().order_by('-created_at')
    return render(request, 'dashboard/standalone/exam_create.html', {
        'available_templates': available_templates,
        'previous_exams': previous_exams,
    })


def standalone_answer_key(request, exam_id):
    """
    Manage Answer Keys for Exam Sets A, B, C, D directly in the dashboard.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)

    if request.method == 'POST':
        set_name = request.POST.get('set_name', 'A').upper()
        answers = {}
        for q in range(1, exam.total_questions + 1):
            val = request.POST.get(f'q_{q}')
            if val:
                answers[str(q)] = val.upper()

        if set_name == 'A':
            exam.answers_set_a = answers
        elif set_name == 'B':
            exam.answers_set_b = answers
        elif set_name == 'C':
            exam.answers_set_c = answers
        elif set_name == 'D':
            exam.answers_set_d = answers

        exam.save()

        # Recalculate existing results if any
        recalc_count = 0
        tpl_path = get_template_path(exam.template_name)
        tpl_data = _get_template_data(tpl_path)
        for res in exam.results.filter(exam_set=set_name):
            ak = exam.get_answer_key(res.exam_set)
            if ak:
                score_res = calculate_score(
                    res.answers_json,
                    ak,
                    correct_mark=exam.correct_mark,
                    wrong_mark=exam.wrong_mark,
                    subjects=tpl_data.get('subjects')
                )
                res.marks = score_res['marks']
                res.subject_marks = score_res.get('subject_marks')
                res.correct_count = score_res['correct']
                res.wrong_count = score_res['wrong']
                res.unattempted_count = score_res['unattempted']
                res.save()
                recalc_count += 1

        if recalc_count > 0:
            exam.recalculate_ranks()
            messages.info(request, f'Set {set_name}-এর {recalc_count}টি পূর্ববর্তী শিটের নম্বর পুনর্মূল্যায়ন করা হয়েছে।')

        messages.success(request, f'Set {set_name}-এর Answer Key সফলভাবে সংরক্ষিত হয়েছে! (মোট উত্তর: {len(answers)}টি)')
        return redirect(f"{request.path}?active_set={set_name}")

    active_set = request.GET.get('active_set', 'A').upper()
    current_key = exam.get_answer_key(active_set)

    import math
    total = exam.total_questions
    cols_count = 4
    per_col = math.ceil(total / cols_count) if total > 0 else 1
    question_columns = []
    for c in range(cols_count):
        start = c * per_col + 1
        end = min((c + 1) * per_col + 1, total + 1)
        if start <= total:
            question_columns.append(list(range(start, end)))

    previous_exams = StandaloneExam.objects.exclude(id=exam.id).order_by('-created_at')

    return render(request, 'dashboard/standalone/answer_key.html', {
        'exam': exam,
        'active_set': active_set,
        'current_key': current_key,
        'questions_range': range(1, exam.total_questions + 1),
        'question_columns': question_columns,
        'previous_exams': previous_exams,
    })


@require_POST
def standalone_copy_keys_ajax(request, exam_id):
    """
    AJAX endpoint to copy answer keys from another StandaloneExam into the current exam.
    Supports copying all sets (A, B, C, D) or only the active set.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    source_exam_id = request.POST.get('source_exam_id')
    copy_scope = request.POST.get('copy_scope', 'all')
    active_set = request.POST.get('active_set', 'A').upper()

    if not source_exam_id:
        return JsonResponse({'success': False, 'error': 'কোনো সোর্স পরীক্ষা নির্বাচন করা হয়নি।'}, status=400)

    try:
        source_exam = StandaloneExam.objects.get(id=int(source_exam_id))
    except (StandaloneExam.DoesNotExist, ValueError):
        return JsonResponse({'success': False, 'error': 'সোর্স পরীক্ষাটি পাওয়া যায়নি।'}, status=404)

    if copy_scope == 'active':
        src_key = source_exam.get_answer_key(active_set)
        if active_set == 'A':
            exam.answers_set_a = src_key or {}
        elif active_set == 'B':
            exam.answers_set_b = src_key or {}
        elif active_set == 'C':
            exam.answers_set_c = src_key or {}
        elif active_set == 'D':
            exam.answers_set_d = src_key or {}
        exam.save()
        msg = f'"{source_exam.title}" থেকে Set {active_set}-এর Answer Key সফলভাবে কপি করা হয়েছে!'
    else:
        exam.answers_set_a = source_exam.answers_set_a or {}
        exam.answers_set_b = source_exam.answers_set_b or {}
        exam.answers_set_c = source_exam.answers_set_c or {}
        exam.answers_set_d = source_exam.answers_set_d or {}
        exam.save()
        msg = f'"{source_exam.title}" থেকে সকল সেট (A, B, C, D)-এর Answer Key সফলভাবে কপি করা হয়েছে!'

    # Recalculate existing results if any
    tpl_path = get_template_path(exam.template_name)
    tpl_data = _get_template_data(tpl_path)
    recalc_count = 0
    for res in exam.results.all():
        ak = exam.get_answer_key(res.exam_set)
        if ak:
            score_res = calculate_score(
                res.answers_json,
                ak,
                correct_mark=exam.correct_mark,
                wrong_mark=exam.wrong_mark,
                subjects=tpl_data.get('subjects')
            )
            res.marks = score_res['marks']
            res.subject_marks = score_res.get('subject_marks')
            res.correct_count = score_res['correct']
            res.wrong_count = score_res['wrong']
            res.unattempted_count = score_res['unattempted']
            res.save()
            recalc_count += 1
    if recalc_count > 0:
        exam.recalculate_ranks()

    return JsonResponse({
        'success': True,
        'message': msg,
        'active_key': exam.get_answer_key(active_set) or {},
        'active_set': active_set
    })


@require_POST
def standalone_scan_key_sheet_ajax(request, exam_id):
    """
    Scan a filled-up master OMR sheet image to automatically extract and populate the Answer Key.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)

    if 'file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'কোনো ছবি আপলোড করা হয়নি।'}, status=400)

    f = request.FILES['file']
    valid_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in valid_exts:
        return JsonResponse({'success': False, 'error': f'অবৈধ ফাইল ফরম্যাট: {ext}। অনুগ্রহ করে ছবি (JPG/PNG) আপলোড করুন।'}, status=400)

    # Save to media/temp_keys/
    temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_keys')
    os.makedirs(temp_dir, exist_ok=True)
    temp_filename = f"master_key_{exam.id}_{int(timezone.now().timestamp())}_{f.name}"
    temp_path = os.path.join(temp_dir, temp_filename)

    with open(temp_path, 'wb+') as destination:
        for chunk in f.chunks():
            destination.write(chunk)

    template_path = get_template_path(exam.template_name, default='offline_exam_100q.json')

    try:
        omr_res = process_sheet(temp_path, template_path, debug=False)
        if omr_res.get('success'):
            raw_answers = omr_res.get('answers', {})
            # Filter and normalize: only questions within 1..exam.total_questions
            answers = {}
            for q_str, ans in raw_answers.items():
                try:
                    q_int = int(q_str)
                    if 1 <= q_int <= exam.total_questions and ans:
                        answers[str(q_int)] = str(ans).upper()
                except (ValueError, TypeError):
                    pass

            detected_set = omr_res.get('exam_set')
            if detected_set:
                detected_set = str(detected_set).upper()

            # Generate public URL for preview if needed
            rel_path = os.path.relpath(temp_path, settings.MEDIA_ROOT).replace('\\', '/')
            image_url = f"{settings.MEDIA_URL}{rel_path}"

            return JsonResponse({
                'success': True,
                'answers': answers,
                'total_filled': len(answers),
                'total_questions': exam.total_questions,
                'detected_set': detected_set,
                'image_url': image_url,
                'message': f"ওএমআর শিট থেকে সফলভাবে {len(answers)}টি উত্তর শনাক্ত করা হয়েছে!"
            })
        else:
            err = omr_res.get('error', 'ওএমআর শিটের কোণা বা বাবল শনাক্ত করতে ব্যর্থ হয়েছে। অনুগ্রহ করে সোজা ও স্পষ্ট স্ক্যান কপি দিন।')
            return JsonResponse({'success': False, 'error': err}, status=400)

    except Exception as e:
        logger.exception("Error processing master answer sheet")
        return JsonResponse({'success': False, 'error': f'প্রসেসিং ত্রুটি: {str(e)}'}, status=500)


def standalone_upload(request, exam_id):
    """
    Upload and evaluate OMR sheets for a standalone exam with AJAX live progress.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    return render(request, 'dashboard/standalone/upload.html', {'exam': exam})


@require_POST
def standalone_upload_single_ajax(request, exam_id):
    """
    Process a single OMR sheet in standalone mode without any API connection:
    - Reads 6-digit Roll directly from sheet.
    - Reads Set (A/B/C/D).
    - Grades against local StandaloneExam answer key.
    - Returns instant evaluated score and rank.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)

    if 'file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'}, status=400)

    f = request.FILES['file']
    valid_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in valid_exts:
        return JsonResponse({'success': False, 'error': f'Invalid file type: {ext}'}, status=400)

    template_path = get_template_path(exam.template_name, default='offline_exam_100q.json')

    # Create result record
    res_obj = StandaloneResult.objects.create(
        exam=exam,
        image_file=f,
        original_filename=f.name,
        roll_no='Processing...'
    )

    try:
        omr_res = process_sheet(res_obj.image_file.path, template_path, debug=False)
        if omr_res.get('success'):
            detected_roll = (omr_res.get('student_numeric_id') or '').strip()

            # Duplicate Roll Protection (Option A: Reject & Alert)
            if detected_roll and detected_roll not in ['অজ্ঞাত', 'শনাক্ত হয়নি', 'Processing...'] and any(c.isdigit() for c in detected_roll):
                existing = StandaloneResult.objects.filter(exam=exam, roll_no=detected_roll).exclude(id=res_obj.id).first()
                if existing:
                    if res_obj.image_file:
                        try:
                            res_obj.image_file.delete(save=False)
                        except Exception:
                            pass
                    res_obj.delete()
                    return JsonResponse({
                        'success': False,
                        'is_duplicate': True,
                        'filename': f.name,
                        'roll_no': detected_roll,
                        'existing_id': existing.id,
                        'existing_marks': round(existing.marks, 2),
                        'existing_set': existing.exam_set,
                        'error': f"রোল {detected_roll} ইতিপূর্বেই স্ক্যান করা হয়েছে (নম্বর: {existing.marks:.2f}, সেট: {existing.exam_set})! ডুপ্লিকেট হিসেবে বাতিল করা হলো।",
                        'error_message': f"রোল {detected_roll} ইতিমধ্যে স্ক্যান করা হয়েছে (নম্বর: {existing.marks:.2f})! ডুপ্লিকেট হিসেবে বাতিল করা হলো।"
                    })

            res_obj.roll_no = detected_roll or 'অজ্ঞাত'
            res_obj.exam_set = (omr_res.get('exam_set') or 'A').upper()
            res_obj.answers_json = omr_res.get('answers', {})

            # Auto-detect if roll_no is a valid Bangladeshi mobile number bubbled by student
            norm_phone = normalize_bd_phone(res_obj.roll_no)
            if norm_phone['is_valid']:
                res_obj.phone_number = norm_phone['local']

            ak = exam.get_answer_key(res_obj.exam_set)
            if ak:
                tpl_data = _get_template_data(template_path)
                score_res = calculate_score(
                    res_obj.answers_json,
                    ak,
                    correct_mark=exam.correct_mark,
                    wrong_mark=exam.wrong_mark,
                    subjects=tpl_data.get('subjects')
                )
                res_obj.marks = score_res['marks']
                res_obj.subject_marks = score_res.get('subject_marks')
                res_obj.correct_count = score_res['correct']
                res_obj.wrong_count = score_res['wrong']
                res_obj.unattempted_count = score_res['unattempted']
            else:
                res_obj.error_message = f"Set {res_obj.exam_set}-এর কোনো Answer Key দেওয়া হয়নি!"
        else:
            res_obj.roll_no = 'শনাক্ত হয়নি'
            res_obj.error_message = omr_res.get('error', 'ওএমআর পড়তে ব্যর্থ')

    except Exception as e:
        logger.exception("Standalone processing error")
        res_obj.roll_no = 'শনাক্ত হয়নি'
        res_obj.error_message = f"প্রসেসিং ত্রুটি: {str(e)}"

    res_obj.save()
    exam.total_sheets = exam.results.count()
    exam.save(update_fields=['total_sheets'])

    return JsonResponse({
        'success': True,
        'result_id': res_obj.id,
        'filename': res_obj.original_filename,
        'roll_no': res_obj.roll_no,
        'exam_set': res_obj.exam_set,
        'marks': round(res_obj.marks, 2),
        'correct_count': res_obj.correct_count,
        'wrong_count': res_obj.wrong_count,
        'unattempted_count': res_obj.unattempted_count,
        'rank': res_obj.rank,
        'error_message': res_obj.error_message,
        'total_evaluated': exam.total_sheets,
    })


def standalone_camera_scan_view(request, exam_id):
    """
    Live Webcam / Mobile Camera Scanning Interface.
    Allows real-time instant scanning and evaluation of OMR sheets.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    template_path = get_template_path(exam.template_name, default='offline_exam_100q.json')
    tpl_data = _get_template_data(template_path)

    # Check which sets have answer keys configured
    sets_configured = {
        'A': bool(exam.answers_set_a),
        'B': bool(exam.answers_set_b),
        'C': bool(exam.answers_set_c),
        'D': bool(exam.answers_set_d),
    }

    recent_results = exam.results.order_by('-created_at')[:10]

    return render(request, 'dashboard/standalone/camera_scan.html', {
        'exam': exam,
        'template_name': exam.template_name,
        'template_data': tpl_data,
        'sets_configured': sets_configured,
        'recent_results': recent_results,
    })


@require_POST
def standalone_camera_scan_ajax(request, exam_id):
    """
    AJAX endpoint to receive an instant camera snapshot (Base64 or file),
    process the OMR sheet, score it, recalculate rank, and save the result.
    """
    import base64
    import uuid
    import time
    from django.core.files.base import ContentFile

    exam = get_object_or_404(StandaloneExam, id=exam_id)
    image_data = request.POST.get('image_data')

    if not image_data and 'file' in request.FILES:
        image_file = request.FILES['file']
        orig_name = image_file.name
    elif image_data:
        try:
            if ';base64,' in image_data:
                format_part, imgstr = image_data.split(';base64,')
                ext = format_part.split('/')[-1]
                if ext == 'jpeg':
                    ext = 'jpg'
            else:
                imgstr = image_data
                ext = 'jpg'

            image_bytes = base64.b64decode(imgstr)
            orig_name = f"cam_{exam.id}_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
            image_file = ContentFile(image_bytes, name=orig_name)
        except Exception as e:
            return JsonResponse({'success': False, 'error': f'ইমেজ ডিকোড ব্যর্থ: {str(e)}'}, status=400)
    else:
        return JsonResponse({'success': False, 'error': 'কোনো ক্যামেরা ইমেজ ফ্রেম পাওয়া যায়নি।'}, status=400)

    template_path = get_template_path(exam.template_name, default='offline_exam_100q.json')

    # Create result record
    res_obj = StandaloneResult.objects.create(
        exam=exam,
        image_file=image_file,
        original_filename=orig_name,
        roll_no='Processing...'
    )

    try:
        omr_res = process_sheet(res_obj.image_file.path, template_path, debug=False)
        if omr_res.get('success'):
            detected_roll = (omr_res.get('student_numeric_id') or '').strip()

            # Duplicate Roll Protection (Option A: Reject & Alert)
            if detected_roll and detected_roll not in ['অজ্ঞাত', 'শনাক্ত হয়নি', 'Processing...'] and any(c.isdigit() for c in detected_roll):
                existing = StandaloneResult.objects.filter(exam=exam, roll_no=detected_roll).exclude(id=res_obj.id).first()
                if existing:
                    if res_obj.image_file:
                        try:
                            res_obj.image_file.delete(save=False)
                        except Exception:
                            pass
                    res_obj.delete()
                    return JsonResponse({
                        'success': False,
                        'is_duplicate': True,
                        'filename': orig_name,
                        'roll_no': detected_roll,
                        'existing_id': existing.id,
                        'existing_marks': round(existing.marks, 2),
                        'existing_set': existing.exam_set,
                        'error': f"রোল {detected_roll} ইতিপূর্বেই স্ক্যান করা হয়েছে (নম্বর: {existing.marks:.2f}, সেট: {existing.exam_set})! ডুপ্লিকেট হিসেবে বাতিল করা হলো।",
                        'error_message': f"রোল {detected_roll} ইতিমধ্যে স্ক্যান করা হয়েছে! ডুপ্লিকেট হিসেবে বাতিল করা হলো।"
                    })

            res_obj.roll_no = detected_roll or 'অজ্ঞাত'
            res_obj.exam_set = (omr_res.get('exam_set') or 'A').upper()
            res_obj.answers_json = omr_res.get('answers', {})

            # Auto-detect if roll_no is a valid Bangladeshi mobile number
            norm_phone = normalize_bd_phone(res_obj.roll_no)
            if norm_phone['is_valid']:
                res_obj.phone_number = norm_phone['local']

            ak = exam.get_answer_key(res_obj.exam_set)
            if ak:
                tpl_data = _get_template_data(template_path)
                score_res = calculate_score(
                    res_obj.answers_json,
                    ak,
                    correct_mark=exam.correct_mark,
                    wrong_mark=exam.wrong_mark,
                    subjects=tpl_data.get('subjects')
                )
                res_obj.marks = score_res['marks']
                res_obj.subject_marks = score_res.get('subject_marks')
                res_obj.correct_count = score_res['correct']
                res_obj.wrong_count = score_res['wrong']
                res_obj.unattempted_count = score_res['unattempted']
            else:
                res_obj.error_message = f"Set {res_obj.exam_set}-এর কোনো Answer Key দেওয়া হয়নি!"

            res_obj.save()
            exam.total_sheets = exam.results.count()
            exam.save(update_fields=['total_sheets'])
            exam.recalculate_ranks()
            res_obj.refresh_from_db()

            return JsonResponse({
                'success': True,
                'result_id': res_obj.id,
                'filename': res_obj.original_filename,
                'roll_no': res_obj.roll_no,
                'exam_set': res_obj.exam_set,
                'marks': round(res_obj.marks, 2),
                'rank': res_obj.rank,
                'correct_count': res_obj.correct_count,
                'wrong_count': res_obj.wrong_count,
                'unattempted_count': res_obj.unattempted_count,
                'auto_flipped': omr_res.get('auto_flipped', False),
                'image_url': res_obj.image_file.url,
                'total_evaluated': exam.total_sheets,
                'message': 'ওএমআর শিট সফলভাবে স্ক্যান ও মূল্যায়ন করা হয়েছে!'
            })
        else:
            res_obj.roll_no = 'শনাক্ত হয়নি'
            res_obj.error_message = omr_res.get('error', 'ওএমআর শিটের বাবল বা মার্কার পড়তে ব্যর্থ')
            res_obj.save()
            return JsonResponse({
                'success': False,
                'result_id': res_obj.id,
                'error': res_obj.error_message,
                'auto_flipped': omr_res.get('auto_flipped', False)
            }, status=400)

    except Exception as e:
        logger.exception("Camera scan processing error")
        res_obj.roll_no = 'শনাক্ত হয়নি'
        res_obj.error_message = f"প্রসেসিং ত্রুটি: {str(e)}"
        res_obj.save()
        return JsonResponse({'success': False, 'error': f"প্রসেসিং ত্রুটি: {str(e)}"}, status=500)


@require_POST
def standalone_finalize_ranks_ajax(request, exam_id):
    """
    Finalizes merit ranks for an exam in a single fast bulk_update operation.
    Called once when all parallel/sequential uploads finish.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    exam.recalculate_ranks()
    return JsonResponse({
        'success': True,
        'total_evaluated': exam.total_sheets
    })


@require_POST
def standalone_regenerate_results_ajax(request, exam_id):
    """
    AJAX endpoint to recalculate scores and merit ranks for all sheets of a standalone exam.
    Useful after editing answer keys, changing marks policy, or fixing misread sets.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    tpl_path = get_template_path(exam.template_name)
    tpl_data = _get_template_data(tpl_path)
    subjects = tpl_data.get('subjects')

    results = exam.results.all()
    rescored_count = 0
    missing_key_sets = set()

    for res in results:
        ak = exam.get_answer_key(res.exam_set)
        if ak:
            score_res = calculate_score(
                res.answers_json,
                ak,
                correct_mark=exam.correct_mark,
                wrong_mark=exam.wrong_mark,
                subjects=subjects
            )
            res.marks = score_res['marks']
            res.subject_marks = score_res.get('subject_marks')
            res.correct_count = score_res['correct']
            res.wrong_count = score_res['wrong']
            res.unattempted_count = score_res['unattempted']
            res.save()
            rescored_count += 1
        else:
            missing_key_sets.add(res.exam_set)

    exam.recalculate_ranks()

    msg = f"মোট {rescored_count}টি শিটের ফলাফল সফলভাবে রিজেনারেট ও মেধা তালিকা (Rank) হালনাগাদ করা হয়েছে!"
    if missing_key_sets:
        msg += f" (সতর্কতা: Set {', '.join(sorted(missing_key_sets))}-এর Answer Key দেওয়া হয়নি।)"

    return JsonResponse({
        'success': True,
        'message': msg,
        'rescored_count': rescored_count,
        'total_count': results.count()
    })


def standalone_results(request, exam_id):
    """View merit list, tabulation, and search by roll number."""
    exam = get_object_or_404(StandaloneExam, id=exam_id)

    # Auto-resolve any stuck 'Processing...' records
    stuck_results = exam.results.filter(roll_no='Processing...')
    if stuck_results.exists():
        template_path = get_template_path(exam.template_name)
        tpl_data = _get_template_data(template_path)
        for stuck in stuck_results:
            try:
                if stuck.image_file and os.path.exists(stuck.image_file.path):
                    omr_res = process_sheet(stuck.image_file.path, template_path, debug=False)
                    if omr_res.get('success'):
                        stuck.roll_no = omr_res.get('student_numeric_id') or 'অজ্ঞাত'
                        stuck.exam_set = (omr_res.get('exam_set') or 'A').upper()
                        stuck.answers_json = omr_res.get('answers', {})
                        norm_phone = normalize_bd_phone(stuck.roll_no)
                        if norm_phone['is_valid']:
                            stuck.phone_number = norm_phone['local']
                        ak = exam.get_answer_key(stuck.exam_set)
                        if ak:
                            score_res = calculate_score(
                                stuck.answers_json,
                                ak,
                                correct_mark=exam.correct_mark,
                                wrong_mark=exam.wrong_mark,
                                subjects=tpl_data.get('subjects')
                            )
                            stuck.marks = score_res['marks']
                            stuck.subject_marks = score_res.get('subject_marks')
                            stuck.correct_count = score_res['correct']
                            stuck.wrong_count = score_res['wrong']
                            stuck.unattempted_count = score_res['unattempted']
                        stuck.error_message = None
                    else:
                        stuck.roll_no = 'শনাক্ত হয়নি'
                        stuck.error_message = omr_res.get('error', 'ওএমআর পড়তে ব্যর্থ')
                else:
                    stuck.roll_no = 'শনাক্ত হয়নি'
                    stuck.error_message = 'মূল স্ক্যান ফাইলটি পাওয়া যায়নি।'
            except Exception as ex:
                stuck.roll_no = 'শনাক্ত হয়নি'
                stuck.error_message = str(ex)
            stuck.save()
        exam.recalculate_ranks()

    # Auto-recalculate ranks if any unranked records exist
    if exam.results.filter(rank=0).exists():
        exam.recalculate_ranks()

    query = request.GET.get('q', '').strip()
    set_filter = request.GET.get('set', '').strip().upper()

    results = exam.results.all()
    if query:
        results = results.filter(roll_no__icontains=query)
    if set_filter in ['A', 'B', 'C', 'D']:
        results = results.filter(exam_set=set_filter)

    # Summary statistics
    total = exam.results.count()
    highest = exam.results.order_by('-marks').first()
    avg_marks = 0
    if total > 0:
        avg_marks = sum(r.marks for r in exam.results.all()) / total

    tpl_path = get_template_path(exam.template_name)
    tpl_data = _get_template_data(tpl_path)
    subjects = tpl_data.get('subjects', [])

    return render(request, 'dashboard/standalone/results.html', {
        'exam': exam,
        'results': results,
        'query': query,
        'set_filter': set_filter,
        'total_count': total,
        'highest_marks': round(highest.marks, 2) if highest else 0,
        'avg_marks': round(avg_marks, 2),
        'subjects': subjects,
    })


def standalone_print(request, exam_id):
    """
    Print-ready Tabulation Sheet / Merit List designed for A4 paper
    with customizable institution branding (name, address, logo, footer).
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    if exam.results.filter(rank=0).exists():
        exam.recalculate_ranks()
    results = exam.results.all().order_by('rank', '-marks', 'roll_no')
    institute = InstituteSettings.get_settings()

    total = results.count()
    highest = results.first()
    avg_marks = (sum(r.marks for r in results) / total) if total > 0 else 0

    tpl_path = get_template_path(exam.template_name)
    tpl_data = _get_template_data(tpl_path)
    subjects = tpl_data.get('subjects', [])

    return render(request, 'dashboard/standalone/print_sheet.html', {
        'exam': exam,
        'results': results,
        'institute': institute,
        'total_count': total,
        'highest_marks': round(highest.marks, 2) if highest else 0,
        'avg_marks': round(avg_marks, 2),
        'print_time': timezone.now(),
        'subjects': subjects,
    })


def standalone_result_detail_ajax(request, result_id):
    """
    AJAX endpoint returning detailed question-by-question student answers vs correct key.
    Fixes the issue where single quotes/None in onclick broke the modal!
    """
    res = get_object_or_404(StandaloneResult, id=result_id)
    exam = res.exam
    ak = exam.get_answer_key(res.exam_set) or {}

    breakdown = []
    for q in range(1, exam.total_questions + 1):
        q_str = str(q)
        student_ans = res.answers_json.get(q_str) or res.answers_json.get(q)
        correct_ans = ak.get(q_str) or ak.get(q)

        c_upper = str(correct_ans).strip().upper() if correct_ans else None
        has_student_ans = bool(student_ans and str(student_ans).strip())

        if not correct_ans:
            status = 'unattempted' if not has_student_ans else 'unknown'
        elif c_upper == 'ANY':
            if has_student_ans:
                status = 'correct'
            else:
                status = 'unattempted'
        elif c_upper == 'NONE':
            if not has_student_ans:
                status = 'correct'
            else:
                status = 'wrong'
        elif not has_student_ans:
            status = 'unattempted'
        elif str(student_ans).strip().upper() == c_upper:
            status = 'correct'
        else:
            status = 'wrong'

        breakdown.append({
            'q': q,
            'student_ans': student_ans,
            'correct_ans': correct_ans,
            'status': status
        })

    return JsonResponse({
        'success': True,
        'id': res.id,
        'roll_no': res.roll_no,
        'exam_set': res.exam_set,
        'marks': round(res.marks, 2),
        'correct_count': res.correct_count,
        'wrong_count': res.wrong_count,
        'unattempted_count': res.unattempted_count,
        'rank': res.rank,
        'total_questions': exam.total_questions,
        'image_url': res.image_file.url if res.image_file else None,
        'breakdown': breakdown
    })


@require_POST
def standalone_result_change_set_ajax(request, result_id):
    """
    AJAX endpoint to correct the exam set of a StandaloneResult (offline).
    Recalculates score and ranks immediately using local SQLite answer keys.
    """
    res = get_object_or_404(StandaloneResult, id=result_id)
    exam = res.exam
    new_set = request.POST.get('exam_set', '').strip().upper()

    if new_set not in ['A', 'B', 'C', 'D']:
        return JsonResponse({'success': False, 'error': 'অবৈধ সেট। শুধু A, B, C অথবা D নির্বাচন করুন।'}, status=400)

    res.exam_set = new_set

    # Recalculate score with local answer key
    ak = exam.get_answer_key(new_set)
    if ak:
        tpl_path = get_template_path(exam.template_name)
        tpl_data = _get_template_data(tpl_path)
        score_res = calculate_score(
            res.answers_json,
            ak,
            correct_mark=exam.correct_mark,
            wrong_mark=exam.wrong_mark,
            subjects=tpl_data.get('subjects')
        )
        res.marks = score_res['marks']
        res.subject_marks = score_res.get('subject_marks')
        res.correct_count = score_res['correct']
        res.wrong_count = score_res['wrong']
        res.unattempted_count = score_res['unattempted']
        res.error_message = None
    else:
        res.marks = 0.0
        res.correct_count = 0
        res.wrong_count = 0
        res.unattempted_count = exam.total_questions
        res.error_message = f"Set {new_set}-এর কোনো Answer Key সংজ্ঞায়িত করা হয়নি!"

    res.save()
    exam.recalculate_ranks()
    res.refresh_from_db()

    return JsonResponse({
        'success': True,
        'id': res.id,
        'roll_no': res.roll_no,
        'exam_set': res.exam_set,
        'marks': round(res.marks, 2),
        'correct_count': res.correct_count,
        'wrong_count': res.wrong_count,
        'unattempted_count': res.unattempted_count,
        'rank': res.rank,
        'error_message': res.error_message,
        'message': f"সেট পরিবর্তন করে Set {new_set} করা হয়েছে এবং নম্বর ও মেধা ক্রম পুনর্মূল্যায়ন সম্পন্ন হয়েছে।"
    })


@require_POST
def standalone_result_edit_ajax(request, result_id):
    """
    AJAX endpoint to edit Roll No and Exam Set for a StandaloneResult (offline).
    """
    res = get_object_or_404(StandaloneResult, id=result_id)
    exam = res.exam
    roll_no = request.POST.get('roll_no', '').strip()
    new_set = request.POST.get('exam_set', '').strip().upper()

    if roll_no:
        res.roll_no = roll_no
    if new_set in ['A', 'B', 'C', 'D']:
        res.exam_set = new_set

    ak = exam.get_answer_key(res.exam_set)
    if ak:
        tpl_path = get_template_path(exam.template_name)
        tpl_data = _get_template_data(tpl_path)
        score_res = calculate_score(
            res.answers_json,
            ak,
            correct_mark=exam.correct_mark,
            wrong_mark=exam.wrong_mark,
            subjects=tpl_data.get('subjects')
        )
        res.marks = score_res['marks']
        res.subject_marks = score_res.get('subject_marks')
        res.correct_count = score_res['correct']
        res.wrong_count = score_res['wrong']
        res.unattempted_count = score_res['unattempted']
        res.error_message = None
    else:
        res.marks = 0.0
        res.correct_count = 0
        res.wrong_count = 0
        res.unattempted_count = exam.total_questions
        res.error_message = f"Set {res.exam_set}-এর কোনো Answer Key সংজ্ঞায়িত করা হয়নি!"

    res.save()
    exam.recalculate_ranks()
    res.refresh_from_db()

    return JsonResponse({
        'success': True,
        'id': res.id,
        'roll_no': res.roll_no,
        'exam_set': res.exam_set,
        'marks': round(res.marks, 2),
        'correct_count': res.correct_count,
        'wrong_count': res.wrong_count,
        'unattempted_count': res.unattempted_count,
        'rank': res.rank,
        'error_message': res.error_message,
        'message': "রোল নম্বর ও সেট সফলভাবে সংরক্ষণ ও পুনর্মূল্যায়ন করা হয়েছে।"
    })


@require_POST
def standalone_reprocess_result_ajax(request, result_id):
    """
    AJAX endpoint to re-process an individual StandaloneResult from its original scan file.
    Solves stuck 'Processing...' or misread sheets in 1 click.
    """
    res = get_object_or_404(StandaloneResult, id=result_id)
    exam = res.exam

    if not res.image_file or not os.path.exists(res.image_file.path):
        res.roll_no = 'শনাক্ত হয়নি'
        res.error_message = 'মূল স্ক্যান ফাইলটি পাওয়া যায়নি।'
        res.save()
        return JsonResponse({'success': False, 'error': 'মূল স্ক্যান ফাইলটি খুঁজে পাওয়া যায়নি।'}, status=404)

    template_path = get_template_path(exam.template_name)
    tpl_data = _get_template_data(template_path)

    try:
        omr_res = process_sheet(res.image_file.path, template_path, debug=False)
        if omr_res.get('success'):
            res.roll_no = omr_res.get('student_numeric_id') or 'অজ্ঞাত'
            res.exam_set = (omr_res.get('exam_set') or 'A').upper()
            res.answers_json = omr_res.get('answers', {})

            norm_phone = normalize_bd_phone(res.roll_no)
            if norm_phone['is_valid']:
                res.phone_number = norm_phone['local']

            ak = exam.get_answer_key(res.exam_set)
            if ak:
                score_res = calculate_score(
                    res.answers_json,
                    ak,
                    correct_mark=exam.correct_mark,
                    wrong_mark=exam.wrong_mark,
                    subjects=tpl_data.get('subjects')
                )
                res.marks = score_res['marks']
                res.subject_marks = score_res.get('subject_marks')
                res.correct_count = score_res['correct']
                res.wrong_count = score_res['wrong']
                res.unattempted_count = score_res['unattempted']
            else:
                res.error_message = f"Set {res.exam_set}-এর কোনো Answer Key দেওয়া হয়নি!"
            res.error_message = None
        else:
            res.roll_no = 'শনাক্ত হয়নি'
            res.error_message = omr_res.get('error', 'ওএমআর পড়তে ব্যর্থ')

        res.save()
        exam.recalculate_ranks()
        res.refresh_from_db()

        return JsonResponse({
            'success': True,
            'id': res.id,
            'roll_no': res.roll_no,
            'exam_set': res.exam_set,
            'marks': round(res.marks, 2),
            'correct_count': res.correct_count,
            'wrong_count': res.wrong_count,
            'unattempted_count': res.unattempted_count,
            'rank': res.rank,
            'error_message': res.error_message,
            'message': f"শিট সফলভাবে পুনঃমূল্যায়ন করা হয়েছে (রোল: {res.roll_no}, সেট: {res.exam_set})।"
        })
    except Exception as e:
        logger.exception("Error re-processing sheet")
        res.roll_no = 'শনাক্ত হয়নি'
        res.error_message = str(e)
        res.save()
        return JsonResponse({'success': False, 'error': f"পুনঃমূল্যায়নে ত্রুটি: {str(e)}"}, status=500)


def get_local_lan_ip():
    """Detect the active local network IPv4 address (e.g. 192.168.1.153)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def generate_qr_data_uri(text):
    """Generate base64 encoded PNG data URI for a QR code."""
    import qrcode, io, base64
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def mobile_qr_ajax(request):
    """Return JSON with local LAN IP and QR code for scanning from mobile."""
    target_path = request.GET.get('path', '/')
    if not target_path.startswith('/'):
        target_path = '/' + target_path

    local_ip = get_local_lan_ip()
    port = request.get_port() or '8000'
    is_ssl = request.is_secure()

    http_url = f"http://{local_ip}:{port}{target_path}"
    https_url = f"https://{local_ip}:{port}{target_path}"

    active_url = https_url if is_ssl else http_url
    active_qr = generate_qr_data_uri(active_url)

    return JsonResponse({
        'success': True,
        'local_ip': local_ip,
        'port': port,
        'is_ssl': is_ssl,
        'active_url': active_url,
        'active_qr': active_qr,
        'http_url': http_url,
        'http_qr': generate_qr_data_uri(http_url),
        'https_url': https_url,
        'https_qr': generate_qr_data_uri(https_url),
    })


def institute_settings_view(request):
    """
    Settings page for institution branding on OMR report prints.
    Name, Address, Helpline, Logo, and Footer Note.
    Also displays dynamic Mobile Connection QR code and LAN IPs.
    """
    settings_obj = InstituteSettings.get_settings()

    if request.method == 'POST':
        settings_obj.institute_name = request.POST.get('institute_name', '').strip() or 'Admission Challenge'
        settings_obj.tagline = request.POST.get('tagline', '').strip()
        settings_obj.address = request.POST.get('address', '').strip()
        settings_obj.phone = request.POST.get('phone', '').strip()
        settings_obj.email = request.POST.get('email', '').strip()
        settings_obj.website = request.POST.get('website', '').strip()
        settings_obj.print_footer_note = request.POST.get('print_footer_note', '').strip()

        if 'logo' in request.FILES:
            settings_obj.logo = request.FILES['logo']

        settings_obj.save()
        messages.success(request, 'প্রতিষ্ঠানের তথ্য ও প্রিন্ট সেটিংস সফলভাবে সংরক্ষিত হয়েছে!')
        return redirect('dashboard:institute_settings')

    local_ip = get_local_lan_ip()
    port = request.get_port() or '8000'
    is_ssl = request.is_secure()

    http_url = f"http://{local_ip}:{port}/"
    https_url = f"https://{local_ip}:{port}/"

    current_url = https_url if is_ssl else http_url
    qr_data_uri = generate_qr_data_uri(current_url)
    http_qr = generate_qr_data_uri(http_url)
    https_qr = generate_qr_data_uri(https_url)

    context = {
        'settings': settings_obj,
        'local_ip': local_ip,
        'port': port,
        'is_ssl': is_ssl,
        'current_url': current_url,
        'http_url': http_url,
        'https_url': https_url,
        'qr_data_uri': qr_data_uri,
        'http_qr': http_qr,
        'https_qr': https_qr,
    }
    return render(request, 'dashboard/settings.html', context)


def standalone_export_csv(request, exam_id):
    """Export tabulation results as CSV file with subject-wise columns when available."""
    import csv
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    results = exam.results.all().order_by('rank', '-marks', 'roll_no')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="exam_{exam.id}_results.csv"'
    response.write('\ufeff'.encode('utf8'))  # BOM for Excel

    tpl_path = get_template_path(exam.template_name)
    tpl_data = _get_template_data(tpl_path)
    subjects = tpl_data.get('subjects', [])

    writer = csv.writer(response)
    header = ['Rank (মেধা ক্রম)', 'Roll No (রোল নম্বর)', 'Exam Set (সেট)']
    if subjects:
        for s in subjects:
            header.append(f"{s['name']} ({s.get('total_questions', '')})")
    header.extend(['Correct (সঠিক)', 'Wrong (ভুল)', 'Unattempted (ছেড়েছে)', 'Total Marks (প্রাপ্ত নম্বর)', 'Percentage'])
    writer.writerow(header)

    for r in results:
        pct = round((r.marks / (exam.total_questions * exam.correct_mark)) * 100, 2) if exam.total_questions > 0 else 0
        row = [r.rank, r.roll_no, r.exam_set]
        if subjects:
            for s in subjects:
                row.append(r.get_subject_mark(s['name']))
        row.extend([r.correct_count, r.wrong_count, r.unattempted_count, r.marks, f"{pct}%"])
        writer.writerow(row)

    return response


@require_POST
def standalone_delete(request, exam_id):
    """Delete an entire standalone exam."""
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    title = exam.title
    exam.delete()
    messages.success(request, f'পরীক্ষা "{title}" মুছে ফেলা হয়েছে।')
    return redirect('dashboard:standalone_list')


@require_POST
def standalone_result_delete(request, result_id):
    """Delete an individual student result from a standalone exam."""
    res = get_object_or_404(StandaloneResult, id=result_id)
    exam = res.exam
    res.delete()
    exam.recalculate_ranks()
    messages.success(request, 'শিক্ষার্থীর ওএমআর রেকর্ড মুছে ফেলা হয়েছে।')
    return redirect('dashboard:standalone_results', exam_id=exam.id)


# ==============================================================================
# USB / GSM MODEM OFFLINE SMS SYSTEM
# ==============================================================================

def modem_settings_view(request):
    """
    Settings page for USB GSM Modem configuration, COM port detection,
    SMS template customization, and live test SMS sending.
    """
    settings_obj = ModemSettings.get_settings()
    available_ports = list_com_ports()

    if request.method == 'POST':
        settings_obj.com_port = request.POST.get('com_port', '').strip()
        settings_obj.baud_rate = int(request.POST.get('baud_rate', 115200))
        settings_obj.delay_seconds = float(request.POST.get('delay_seconds', 2.5))
        settings_obj.is_enabled = request.POST.get('is_enabled') == 'on'
        settings_obj.sms_template = request.POST.get('sms_template', '').strip()
        settings_obj.save()
        messages.success(request, 'মডেম ও এসএমএস সেটিংস সফলভাবে সংরক্ষিত হয়েছে!')
        return redirect('dashboard:modem_settings')

    return render(request, 'dashboard/modem_settings.html', {
        'settings': settings_obj,
        'available_ports': available_ports
    })


@require_POST
def modem_detect_ajax(request):
    """AJAX endpoint to scan and return available COM ports."""
    ports = list_com_ports()
    return JsonResponse({'success': True, 'ports': ports})


@require_POST
def modem_test_ajax(request):
    """AJAX endpoint to test modem AT communication and signal strength."""
    port = request.POST.get('com_port', '').strip()
    baud_rate = int(request.POST.get('baud_rate', 115200))
    res = test_modem(port, baudrate=baud_rate)
    if res.get('success'):
        # Automatically persist the tested and working port so the user doesn't have to worry about manual save
        settings_obj = ModemSettings.get_settings()
        settings_obj.com_port = port
        settings_obj.baud_rate = baud_rate
        settings_obj.save(update_fields=['com_port', 'baud_rate'])
    return JsonResponse(res)


@require_POST
def modem_test_send_ajax(request):
    """AJAX endpoint to send a single test SMS."""
    settings_obj = ModemSettings.get_settings()
    port = request.POST.get('com_port', '').strip() or settings_obj.com_port
    baud_rate = int(request.POST.get('baud_rate', settings_obj.baud_rate))
    recipient = request.POST.get('recipient', '').strip()
    message = request.POST.get('message', '').strip()

    if not port:
        return JsonResponse({'success': False, 'error': 'COM পোর্ট নির্বাচন করুন'}, status=400)
    if not recipient:
        return JsonResponse({'success': False, 'error': 'প্রাপকের মোবাইল নম্বর লিখুন'}, status=400)
    if not message:
        return JsonResponse({'success': False, 'error': 'এসএমএস বার্তা লিখুন'}, status=400)

    res = send_sms_via_modem(port, baud_rate, recipient, message)
    if res.get('success') and port != settings_obj.com_port:
        settings_obj.com_port = port
        settings_obj.baud_rate = baud_rate
        settings_obj.save(update_fields=['com_port', 'baud_rate'])
    return JsonResponse(res)


def standalone_sms_preview_ajax(request, exam_id):
    """
    Returns preview data for sending SMS to an exam's students:
    - Total students evaluated
    - Count of students with valid mobile numbers (from OMR bubble or manual)
    - Full list of student queue items for sequential progress
    - Sample rendered SMS message
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    settings_obj = ModemSettings.get_settings()
    institute = InstituteSettings.get_settings()

    results = exam.results.all().order_by('rank', '-marks')
    total_students = results.count()

    ready_count = 0
    missing_count = 0
    sent_count = 0
    failed_count = 0
    pending_count = 0
    students_queue = []
    preview_sample = None

    for r in results:
        target_phone = r.phone_number or r.roll_no
        norm = normalize_bd_phone(target_phone)
        is_valid_phone = norm['is_valid']

        if is_valid_phone:
            ready_count += 1
            if r.sms_status == 'sent':
                sent_count += 1
            elif r.sms_status == 'failed':
                failed_count += 1
                pending_count += 1
            else:
                pending_count += 1

            if preview_sample is None:
                rendered_text = render_sms_template(settings_obj.sms_template, exam, r, institute)
                preview_sample = {
                    'phone': norm['local'],
                    'text': rendered_text
                }
        else:
            missing_count += 1

        students_queue.append({
            'id': r.id,
            'roll': r.roll_no,
            'phone': norm['local'] if is_valid_phone else '',
            'status': r.sms_status or 'pending',
            'marks': round(r.marks, 2),
            'rank': r.rank
        })

    return JsonResponse({
        'success': True,
        'exam_title': exam.title,
        'total_students': total_students,
        'total_count': total_students,
        'ready_count': ready_count,
        'valid_phone_count': ready_count,
        'missing_count': missing_count,
        'no_phone_count': missing_count,
        'sent_count': sent_count,
        'failed_count': failed_count,
        'pending_count': pending_count,
        'modem_configured': bool(settings_obj.com_port),
        'port': settings_obj.com_port,
        'is_enabled': settings_obj.is_enabled,
        'students': students_queue,
        'preview_sample': preview_sample,
        'sms_template': settings_obj.sms_template
    })


@require_POST
def standalone_send_single_sms_by_result_id_ajax(request, result_id):
    """
    Alias AJAX endpoint to send SMS by result_id without exam_id in URL.
    """
    result = get_object_or_404(StandaloneResult, id=result_id)
    return standalone_send_single_sms_ajax(request, result.exam.id, result.id)


@require_POST
def standalone_send_single_sms_ajax(request, exam_id, result_id):
    """
    Sends an SMS to a single student result using the configured GSM modem.
    Called sequentially by the client-side progress worker.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    result = get_object_or_404(StandaloneResult, id=result_id, exam=exam)
    settings_obj = ModemSettings.get_settings()
    institute = InstituteSettings.get_settings()

    if not settings_obj.com_port:
        return JsonResponse({'success': False, 'error': 'মডেমের COM পোর্ট কনফিগার করা নেই। সেটিংস থেকে পোর্ট সিলেক্ট করুন।'}, status=400)

    target_phone = result.phone_number or result.roll_no
    norm = normalize_bd_phone(target_phone)
    if not norm['is_valid']:
        result.sms_status = 'failed'
        result.sms_error = f"অকার্যকর মোবাইল নম্বর ({target_phone})"
        result.save(update_fields=['sms_status', 'sms_error'])
        return JsonResponse({
            'success': False,
            'result_id': result.id,
            'roll_no': result.roll_no,
            'phone': target_phone,
            'error': result.sms_error
        })

    # Render SMS
    rendered_text = render_sms_template(settings_obj.sms_template, exam, result, institute)

    # Send via modem
    res = send_sms_via_modem(
        port=settings_obj.com_port,
        baudrate=settings_obj.baud_rate,
        recipient=norm['international'],
        text=rendered_text
    )

    if res['success']:
        result.phone_number = norm['local']
        result.sms_status = 'sent'
        result.sms_sent_at = timezone.now()
        result.sms_error = None
        result.save(update_fields=['phone_number', 'sms_status', 'sms_sent_at', 'sms_error'])

        # Save to SmsLog audit history
        SmsLog.objects.create(
            exam=exam,
            result=result,
            recipient=norm['local'],
            roll_no=result.roll_no,
            student_name=result.student_name,
            message_text=rendered_text,
            status='sent',
            sent_at=result.sms_sent_at
        )

        return JsonResponse({
            'success': True,
            'result_id': result.id,
            'roll_no': result.roll_no,
            'phone': norm['local'],
            'sent_at': result.sms_sent_at.strftime('%d %b, %I:%M %p'),
            'message_id': res.get('message_id')
        })
    else:
        result.phone_number = norm['local']
        result.sms_status = 'failed'
        result.sms_error = res.get('error')
        result.save(update_fields=['phone_number', 'sms_status', 'sms_error'])

        # Save to SmsLog audit history
        SmsLog.objects.create(
            exam=exam,
            result=result,
            recipient=norm['local'],
            roll_no=result.roll_no,
            student_name=result.student_name,
            message_text=rendered_text,
            status='failed',
            error_message=res.get('error')
        )

        return JsonResponse({
            'success': False,
            'result_id': result.id,
            'roll_no': result.roll_no,
            'phone': norm['local'],
            'error': res.get('error')
        })


@require_POST
def standalone_import_contacts_ajax(request, exam_id):
    """
    (Optional) Import Student Names and/or Mobile Numbers from Excel/CSV.
    Format: Columns containing Roll, Name, Phone / Mobile.
    Updates matching StandaloneResult records by roll_no.
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    if 'file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'কোনো ফাইল নির্বাচন করা হয়নি।'}, status=400)

    f = request.FILES['file']
    filename = f.name.lower()
    updated_count = 0

    try:
        import csv
        import io

        rows = []
        if filename.endswith('.csv'):
            decoded = f.read().decode('utf-8-sig', errors='ignore')
            reader = csv.reader(io.StringIO(decoded))
            rows = list(reader)
        elif filename.endswith(('.xls', '.xlsx')):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(f, data_only=True)
                sheet = wb.active
                for r in sheet.iter_rows(values_only=True):
                    rows.append([str(c or '').strip() for c in r])
            except ImportError:
                return JsonResponse({'success': False, 'error': 'Excel ফাইল পড়তে openpyxl প্রয়োজন। অনুগ্রহ করে .csv ফরম্যাটে আপলোড করুন।'}, status=400)

        if not rows:
            return JsonResponse({'success': False, 'error': 'ফাইলের ভেতর কোনো ডাটা পাওয়া যায়নি।'}, status=400)

        header = [str(h).lower() for h in rows[0]]
        roll_idx = -1
        name_idx = -1
        phone_idx = -1

        for idx, col in enumerate(header):
            if any(k in col for k in ['roll', 'রোল', 'id', 'student_id']):
                roll_idx = idx
            elif any(k in col for k in ['name', 'নাম', 'student_name']):
                name_idx = idx
            elif any(k in col for k in ['phone', 'mobile', 'ফোন', 'মোবাইল', 'contact']):
                phone_idx = idx

        if roll_idx == -1:
            roll_idx = 0
            if len(header) > 1:
                name_idx = 1
            if len(header) > 2:
                phone_idx = 2
            start_row = 0
        else:
            start_row = 1

        for row in rows[start_row:]:
            if not row or len(row) <= roll_idx:
                continue
            raw_roll = str(row[roll_idx]).strip()
            if not raw_roll:
                continue

            res_matches = exam.results.filter(roll_no=raw_roll)
            if not res_matches.exists():
                digits_roll = re.sub(r'\D', '', raw_roll)
                res_matches = exam.results.filter(roll_no=digits_roll)

            for res in res_matches:
                changed = False
                if name_idx >= 0 and len(row) > name_idx and row[name_idx]:
                    res.student_name = str(row[name_idx]).strip()
                    changed = True
                if phone_idx >= 0 and len(row) > phone_idx and row[phone_idx]:
                    norm = normalize_bd_phone(row[phone_idx])
                    if norm['is_valid']:
                        res.phone_number = norm['local']
                        changed = True
                if changed:
                    res.save()
                    updated_count += 1

        return JsonResponse({
            'success': True,
            'updated_count': updated_count,
            'message': f"মোট {updated_count} জন শিক্ষার্থীর তথ্য আপডেট করা হয়েছে।"
        })

    except Exception as e:
        logger.exception("Contact import error")
        return JsonResponse({'success': False, 'error': f"ফাইল প্রসেসিং ব্যর্থ: {e}"}, status=500)


# ==============================================================================
# BACKGROUND SMS CAMPAIGN & SMS HISTORY CONTROLLERS
# ==============================================================================

@require_POST
def standalone_start_sms_campaign_ajax(request, exam_id):
    """
    Initiates a server-side background SMS campaign for an exam.
    The worker runs in an independent background thread, so the user can freely
    leave the page or close the tab without terminating ongoing SMS sends.
    """
    filter_option = request.POST.get('filter_option', 'pending')
    ok, err = start_exam_sms_campaign(exam_id, filter_option=filter_option)
    if ok:
        return JsonResponse({
            'success': True,
            'message': 'ব্যাকগ্রাউন্ডে এসএমএস প্রেরণ শুরু হয়েছে। আপনি চাইলে অন্য পেজে যেতে পারবেন, প্রেরণ অব্যাহত থাকবে।'
        })
    return JsonResponse({'success': False, 'error': err}, status=400)


@require_POST
def standalone_stop_sms_campaign_ajax(request, exam_id):
    """
    Requests an active background SMS campaign to stop.
    """
    stopped = stop_exam_sms_campaign(exam_id)
    return JsonResponse({
        'success': True,
        'message': 'এসএমএস প্রেরণ স্থগিত করা হয়েছে।' if stopped else 'কোনো সক্রিয় ক্যাম্পেইন চালু নেই।'
    })


def standalone_sms_campaign_status_ajax(request, exam_id):
    """
    Polls the real-time server-side status of an exam's SMS campaign.
    """
    status_data = get_campaign_status(exam_id)
    if not status_data:
        exam = get_object_or_404(StandaloneExam, id=exam_id)
        results = exam.results.all()
        sent = results.filter(sms_status='sent').count()
        failed = results.filter(sms_status='failed').count()
        total = results.count()
        return JsonResponse({
            'is_running': False,
            'total': total,
            'sent_count': sent,
            'failed_count': failed,
            'remaining_count': max(0, total - sent - failed),
            'percent': 0,
            'logs': []
        })
    return JsonResponse(status_data)


def sms_history_view(request):
    """
    Displays full audit history of SMS deliveries across all exams or a specific exam.
    Supports filtering by status (all, sent, failed), search by phone/roll, pagination,
    and bulk/individual retry of failed messages.
    """
    from django.core.paginator import Paginator

    exams = StandaloneExam.objects.all().order_by('-created_at')
    status_filter = request.GET.get('status', '').strip().lower()
    exam_filter = request.GET.get('exam_id', '').strip()
    search_query = request.GET.get('q', '').strip()

    logs_qs = SmsLog.objects.select_related('exam', 'result').all()

    if status_filter in ['sent', 'failed', 'pending']:
        logs_qs = logs_qs.filter(status=status_filter)

    if exam_filter.isdigit():
        logs_qs = logs_qs.filter(exam_id=int(exam_filter))

    if search_query:
        logs_qs = logs_qs.filter(
            Q(recipient__icontains=search_query) |
            Q(roll_no__icontains=search_query) |
            Q(student_name__icontains=search_query)
        )

    # Summary Statistics
    base_qs = SmsLog.objects.all()
    if exam_filter.isdigit():
        base_qs = base_qs.filter(exam_id=int(exam_filter))

    total_count = base_qs.count()
    sent_count = base_qs.filter(status='sent').count()
    failed_count = base_qs.filter(status='failed').count()
    pending_count = base_qs.filter(status='pending').count()
    success_rate = round((sent_count / max(total_count, 1)) * 100, 1)

    # 40 items per page
    paginator = Paginator(logs_qs, 40)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'dashboard/sms_history.html', {
        'page_obj': page_obj,
        'exams': exams,
        'current_status': status_filter,
        'current_exam_id': exam_filter,
        'search_query': search_query,
        'total_count': total_count,
        'sent_count': sent_count,
        'failed_count': failed_count,
        'pending_count': pending_count,
        'success_rate': success_rate,
    })


@require_POST
def sms_retry_single_ajax(request, log_id):
    """
    Retries sending an individual failed SMS from history.
    """
    log = get_object_or_404(SmsLog, id=log_id)
    modem_settings = ModemSettings.get_settings()
    institute = InstituteSettings.get_settings()

    if not modem_settings.com_port:
        return JsonResponse({'success': False, 'error': 'মডেমের COM পোর্ট কনফিগার করা নেই।'}, status=400)

    norm = normalize_bd_phone(log.recipient)
    if not norm['is_valid']:
        log.status = 'failed'
        log.error_message = f"অকার্যকর মোবাইল নম্বর ({log.recipient})"
        log.save(update_fields=['status', 'error_message'])
        return JsonResponse({'success': False, 'error': log.error_message})

    msg_text = log.message_text
    if (not msg_text or msg_text == '(বার্তা পাঠানো যায়নি)') and log.result and log.exam:
        msg_text = render_sms_template(modem_settings.sms_template, log.exam, log.result, institute)
        log.message_text = msg_text

    sms_res = send_sms_via_modem(
        port=modem_settings.com_port,
        baudrate=modem_settings.baud_rate,
        recipient=norm['international'],
        text=msg_text
    )

    log.retry_count += 1
    if sms_res.get('success'):
        log.status = 'sent'
        log.sent_at = timezone.now()
        log.error_message = None
        log.save(update_fields=['status', 'sent_at', 'error_message', 'retry_count', 'message_text'])

        if log.result:
            log.result.sms_status = 'sent'
            log.result.sms_sent_at = timezone.now()
            log.result.sms_error = None
            log.result.save(update_fields=['sms_status', 'sms_sent_at', 'sms_error'])

        return JsonResponse({
            'success': True,
            'message': f"রোল {log.roll_no} ({log.recipient})-এ পুনরায় এসএমএস সফলভাবে পাঠানো হয়েছে!",
            'sent_at': log.sent_at.strftime('%d %b, %I:%M %p')
        })
    else:
        log.status = 'failed'
        log.error_message = sms_res.get('error', 'মোডেম যোগাযোগ ত্রুটি')
        log.save(update_fields=['status', 'error_message', 'retry_count', 'message_text'])

        if log.result:
            log.result.sms_status = 'failed'
            log.result.sms_error = log.error_message
            log.result.save(update_fields=['sms_status', 'sms_error'])

        return JsonResponse({'success': False, 'error': log.error_message})


@require_POST
def sms_retry_all_failed_ajax(request):
    """
    Retries sending all failed SMS in a background worker thread.
    Can be filtered by exam_id if provided.
    """
    import threading
    exam_id = request.POST.get('exam_id')
    qs = SmsLog.objects.filter(status='failed')
    if exam_id and exam_id.isdigit():
        qs = qs.filter(exam_id=int(exam_id))

    failed_ids = list(qs.values_list('id', flat=True))
    if not failed_ids:
        return JsonResponse({'success': False, 'error': 'পুনরায় পাঠানোর মতো কোনো ব্যর্থ এসএমএস নেই।'})

    modem_settings = ModemSettings.get_settings()
    institute = InstituteSettings.get_settings()

    if not modem_settings.com_port:
        return JsonResponse({'success': False, 'error': 'মডেমের COM পোর্ট কনফিগার করা নেই।'}, status=400)

    def _retry_worker(log_ids, port, baud, delay, tpl):
        connection.close()
        for lid in log_ids:
            try:
                item = SmsLog.objects.select_related('exam', 'result').get(id=lid)
                norm = normalize_bd_phone(item.recipient)
                if not norm['is_valid']:
                    continue
                msg = item.message_text
                if (not msg or msg == '(বার্তা পাঠানো যায়নি)') and item.result and item.exam:
                    msg = render_sms_template(tpl, item.exam, item.result, institute)
                    item.message_text = msg

                res = send_sms_via_modem(port=port, baudrate=baud, recipient=norm['international'], text=msg)
                item.retry_count += 1
                if res.get('success'):
                    item.status = 'sent'
                    item.sent_at = timezone.now()
                    item.error_message = None
                    item.save()
                    if item.result:
                        item.result.sms_status = 'sent'
                        item.result.sms_sent_at = timezone.now()
                        item.result.sms_error = None
                        item.result.save(update_fields=['sms_status', 'sms_sent_at', 'sms_error'])
                else:
                    item.status = 'failed'
                    item.error_message = res.get('error')
                    item.save()
                time.sleep(delay)
            except Exception as e:
                logger.exception(f"Error retrying SMS log {lid}: {e}")
        connection.close()

    threading.Thread(
        target=_retry_worker,
        args=(
            failed_ids,
            modem_settings.com_port,
            modem_settings.baud_rate,
            modem_settings.delay_seconds,
            modem_settings.sms_template
        ),
        daemon=True
    ).start()

    return JsonResponse({
        'success': True,
        'message': f"মোট {len(failed_ids)}টি ব্যর্থ এসএমএস ব্যাকগ্রাউন্ডে পুনরায় পাঠানো শুরু হয়েছে। আপনি চাইলে অন্য পেজে যেতে পারেন।"
    })


@require_POST
def sms_clear_history_ajax(request):
    """Clears SMS logs (e.g. older logs or all)."""
    filter_type = request.POST.get('filter_type', 'all')
    if filter_type == 'sent':
        count, _ = SmsLog.objects.filter(status='sent').delete()
    elif filter_type == 'failed':
        count, _ = SmsLog.objects.filter(status='failed').delete()
    else:
        count, _ = SmsLog.objects.all().delete()
    return JsonResponse({'success': True, 'message': f'মোট {count}টি হিস্ট্রি লগ মুছে ফেলা হয়েছে।'})


# ==============================================================================
# OMR SHEET DESIGNER & PRINT-READY PDF GENERATOR
# ==============================================================================
def sheet_designer_view(request):
    """
    Visual OMR Sheet Designer & Generator.
    Lets users design custom A4 OMR sheets for printing (offset press / office printer),
    with live preview, export to printable PDF, and auto-sync evaluation template.
    """
    inst_settings = InstituteSettings.get_settings()
    context = {
        'inst_settings': inst_settings,
        'default_title': inst_settings.institute_name or 'Admission Challenge',
        'default_tagline': inst_settings.tagline or 'বিশ্ববিদ্যালয় ভর্তি কোচিং',
    }
    return render(request, 'dashboard/sheet_designer.html', context)


@xframe_options_sameorigin
def sheet_print_view(request):
    """
    Standalone clean print view for the designed OMR sheet.
    Rendered with precise A4 dimensions, corner alignment squares, and clean styling.
    """
    inst_settings = InstituteSettings.get_settings()
    context = {
        'inst_settings': inst_settings,
        'autoprint': request.GET.get('autoprint', '0') == '1',
    }
    return render(request, 'dashboard/sheet_print.html', context)


@require_POST
def sheet_designer_save_template_ajax(request):
    """
    Saves the designed OMR sheet's coordinate specification into templates_store/
    so the OMR Engine can immediately evaluate sheets printed from this design.
    """
    try:
        data = json.loads(request.body)
        raw_name = data.get('template_name', '').strip() or 'custom_omr_sheet'
        # Sanitize filename
        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_name).strip('_').lower()
        if not safe_name:
            safe_name = f"omr_sheet_{int(time.time())}"
        if not safe_name.endswith('.json'):
            safe_name += '.json'

        template_payload = {
            "name": data.get('title', 'Custom Designed OMR Sheet'),
            "version": "1.0",
            "dimensions": data.get('dimensions', {"width": 794, "height": 1123}),
            "orientation": "portrait",
            "corner_markers": {
                "min_area": 100,
                "max_area": 10000,
                "aspect_tolerance": 0.4
            },
            "id_grid": data.get('id_grid'),
            "set_region": data.get('set_region'),
            "answer_columns": data.get('answer_columns', [])
        }

        templates_dir = Path(settings.BASE_DIR) / 'templates_store'
        templates_dir.mkdir(parents=True, exist_ok=True)
        target_path = templates_dir / safe_name

        with open(target_path, 'w', encoding='utf-8') as f:
            json.dump(template_payload, f, indent=2, ensure_ascii=False)

        logger.info(f"Custom OMR template saved: {target_path}")
        return JsonResponse({
            'success': True,
            'template_name': safe_name,
            'message': f"ওএমআর টেমপ্লেট '{safe_name}' সফলভাবে সংরক্ষিত হয়েছে! নতুন পরীক্ষার তালিকায় এটি এখনই ব্যবহার করা যাবে।"
        })
    except Exception as e:
        logger.exception("Failed to save custom OMR template")
        return JsonResponse({'success': False, 'error': f"টেমপ্লেট সেভ ত্রুটি: {str(e)}"}, status=500)





