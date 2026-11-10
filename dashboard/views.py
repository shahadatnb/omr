import os
import json
import logging
from pathlib import Path
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.conf import settings
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import BatchJob, ScanResult, StandaloneExam, StandaloneResult, InstituteSettings
from omr_engine.sheet_processor import process_sheet
from omr_engine.scorer import calculate_score
from integration.laravel_client import LaravelOmrClient

logger = logging.getLogger(__name__)

# In-memory answer key cache to minimize redundant Laravel calls per set
_ANSWER_KEY_CACHE = {}


def _get_cached_answer_key(client, class_test_id, exam_set):
    cache_key = f"{class_test_id}_{exam_set.upper()}"
    if cache_key in _ANSWER_KEY_CACHE:
        return _ANSWER_KEY_CACHE[cache_key]
    try:
        ak = client.get_answer_key(class_test_id, exam_set)
        if ak and 'answers' in ak:
            _ANSWER_KEY_CACHE[cache_key] = ak
            return ak
    except Exception as e:
        logger.warning(f"Failed to fetch answer key for Set {exam_set}: {e}")
    return None


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
                    score_res = calculate_score(
                        scan.answers_json,
                        ak['answers'],
                        correct_mark=ak.get('correct_mark', 1.0),
                        wrong_mark=ak.get('wrong_mark', -0.25)
                    )
                    scan.marks = score_res['marks']
                    scan.correct_count = score_res['correct']
                    scan.wrong_count = score_res['wrong']
                    scan.unattempted_count = score_res['unattempted']

            # Match student with Laravel
            if scan.student_numeric_id:
                try:
                    student_data = client.find_student(batch.class_test_id, scan.student_numeric_id)
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
                except Exception as e:
                    scan.is_matched = False
                    scan.status = 'unmatched'
                    scan.error_message = f"লারাভেল লুকআপ ব্যর্থ: {e}"
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

    return render(request, 'dashboard/batch_detail.html', {
        'batch': batch,
        'results': results,
        'filter_type': filter_type,
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
            result.exam_set = exam_set
            result.is_matched = True
            result.is_manually_edited = True
            result.status = 'manual'
            result.error_message = None

            # Recalculate score with the chosen set
            try:
                ak = _get_cached_answer_key(client, batch.class_test_id, exam_set)
                if ak and 'answers' in ak:
                    score_res = calculate_score(
                        result.answers_json,
                        ak['answers'],
                        correct_mark=ak.get('correct_mark', 1.0),
                        wrong_mark=ak.get('wrong_mark', -0.25)
                    )
                    result.marks = score_res['marks']
                    result.correct_count = score_res['correct']
                    result.wrong_count = score_res['wrong']
                    result.unattempted_count = score_res['unattempted']
            except Exception as ex:
                logger.warning(f"Error recalculating score during manual edit: {ex}")

            result.save()
            batch.update_stats()

            messages.success(request, f"Sheet manually mapped to student: {result.student_name} ({result.student_reg_no}). Score: {result.marks}")
            return redirect('dashboard:batch_detail', batch_id=batch.id)
        else:
            messages.error(request, 'Please select a student from the list.')

    return render(request, 'dashboard/manual_edit.html', {
        'result': result,
        'batch': batch,
        'enrolled_students': enrolled_students,
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
        payload.append({
            'item_id': r.item_id,
            'marks': r.marks,
            'correct_answer': r.correct_count,
            'wrong_answer': r.wrong_count,
            'is_absend': 0,
        })

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

        # Detect corners and warp
        corners = find_corner_markers(img)
        corners_found = False
        if corners:
            warped = warp_sheet(img, corners, output_size=(794, 1123))
            corners_found = True
        else:
            warped = cv2.resize(img, (794, 1123))

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
            # Pick first 15 answers
            answers_sample = {str(k): res['answers'][k] for k in list(res['answers'].keys())[:15]} if res.get('answers') else {}
            filled_count = sum(1 for v in res.get('answers', {}).values() if v is not None)

            return JsonResponse({
                'success': res.get('success', False),
                'student_numeric_id': res.get('student_numeric_id'),
                'exam_set': res.get('exam_set'),
                'answered_count': filled_count,
                'total_questions': len(res.get('answers', {})),
                'sample_answers': answers_sample,
                'error': res.get('error')
            })
        finally:
            if os.path.exists(temp_template_path):
                os.remove(temp_template_path)

    except Exception as e:
        logger.exception("Template test error")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ==============================================================================
# STANDALONE / OFFLINE OMR EXAM SYSTEM (NO API DEPENDENCY)
# ==============================================================================

def standalone_list(request):
    """List all standalone offline exams."""
    exams = StandaloneExam.objects.all().order_by('-created_at')
    return render(request, 'dashboard/standalone/exam_list.html', {'exams': exams})


def standalone_create(request):
    """Create a new standalone exam with scoring rules and selected OMR template."""
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        exam_date = request.POST.get('exam_date')
        total_questions = int(request.POST.get('total_questions', 100))
        correct_mark = float(request.POST.get('correct_mark', 1.0))
        wrong_mark = float(request.POST.get('wrong_mark', -0.25))
        template_name = request.POST.get('template_name', 'offline_exam_100q.json').strip() or 'offline_exam_100q.json'

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
        messages.success(request, f'পরীক্ষা "{exam.title}" তৈরি হয়েছে। এখন Answer Key সেট করুন।')
        return redirect('dashboard:standalone_answer_key', exam_id=exam.id)

    available_templates = get_available_templates()
    return render(request, 'dashboard/standalone/exam_create.html', {
        'available_templates': available_templates
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
        for res in exam.results.filter(exam_set=set_name):
            ak = exam.get_answer_key(res.exam_set)
            if ak:
                score_res = calculate_score(
                    res.answers_json,
                    ak,
                    correct_mark=exam.correct_mark,
                    wrong_mark=exam.wrong_mark
                )
                res.marks = score_res['marks']
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

    return render(request, 'dashboard/standalone/answer_key.html', {
        'exam': exam,
        'active_set': active_set,
        'current_key': current_key,
        'questions_range': range(1, exam.total_questions + 1),
    })


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
            res_obj.roll_no = omr_res.get('student_numeric_id') or 'অজ্ঞাত'
            res_obj.exam_set = (omr_res.get('exam_set') or 'A').upper()
            res_obj.answers_json = omr_res.get('answers', {})

            ak = exam.get_answer_key(res_obj.exam_set)
            if ak:
                score_res = calculate_score(
                    res_obj.answers_json,
                    ak,
                    correct_mark=exam.correct_mark,
                    wrong_mark=exam.wrong_mark
                )
                res_obj.marks = score_res['marks']
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
        res_obj.error_message = str(e)

    res_obj.save()
    exam.recalculate_ranks()
    res_obj.refresh_from_db()

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


def standalone_results(request, exam_id):
    """View merit list, tabulation, and search by roll number."""
    exam = get_object_or_404(StandaloneExam, id=exam_id)
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

    return render(request, 'dashboard/standalone/results.html', {
        'exam': exam,
        'results': results,
        'query': query,
        'set_filter': set_filter,
        'total_count': total,
        'highest_marks': round(highest.marks, 2) if highest else 0,
        'avg_marks': round(avg_marks, 2),
    })


def standalone_print(request, exam_id):
    """
    Print-ready Tabulation Sheet / Merit List designed for A4 paper
    with customizable institution branding (name, address, logo, footer).
    """
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    results = exam.results.all().order_by('rank', '-marks', 'roll_no')
    institute = InstituteSettings.get_settings()

    total = results.count()
    highest = results.first()
    avg_marks = (sum(r.marks for r in results) / total) if total > 0 else 0

    return render(request, 'dashboard/standalone/print_sheet.html', {
        'exam': exam,
        'results': results,
        'institute': institute,
        'total_count': total,
        'highest_marks': round(highest.marks, 2) if highest else 0,
        'avg_marks': round(avg_marks, 2),
        'print_time': timezone.now(),
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

        if not student_ans:
            status = 'unattempted'
        elif correct_ans and student_ans.upper() == correct_ans.upper():
            status = 'correct'
        elif correct_ans:
            status = 'wrong'
        else:
            status = 'unknown'

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


def institute_settings_view(request):
    """
    Settings page for institution branding on OMR report prints.
    Name, Address, Helpline, Logo, and Footer Note.
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

    return render(request, 'dashboard/settings.html', {'settings': settings_obj})


def standalone_export_csv(request, exam_id):
    """Export tabulation results as CSV file."""
    import csv
    exam = get_object_or_404(StandaloneExam, id=exam_id)
    results = exam.results.all().order_by('rank', '-marks', 'roll_no')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="exam_{exam.id}_results.csv"'
    response.write('\ufeff'.encode('utf8'))  # BOM for Excel

    writer = csv.writer(response)
    writer.writerow(['Rank (মেধা ক্রম)', 'Roll No (রোল নম্বর)', 'Exam Set (সেট)', 'Correct (সঠিক)', 'Wrong (ভুল)', 'Unattempted (ছেড়েছে)', 'Total Marks (প্রাপ্ত নম্বর)', 'Percentage'])

    for r in results:
        pct = round((r.marks / (exam.total_questions * exam.correct_mark)) * 100, 2) if exam.total_questions > 0 else 0
        writer.writerow([r.rank, r.roll_no, r.exam_set, r.correct_count, r.wrong_count, r.unattempted_count, r.marks, f"{pct}%"])

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


