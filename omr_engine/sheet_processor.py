"""
omr_engine/sheet_processor.py
Main OMR sheet processing pipeline.

এই মডিউলটি সব sub-module একসাথে ব্যবহার করে
একটি OMR শিট সম্পূর্ণভাবে প্রসেস করে।

Processing Pipeline:
1. Image load → 2. Perspective correction → 3. ID read
→ 4. Set read → 5. Answer read → 6. Score calculate
"""

import cv2
import json
import logging
import numpy as np
from pathlib import Path
from datetime import datetime

from .perspective import find_corner_markers, warp_sheet, preprocess_image
from .id_reader import read_id_number, read_id_with_debug
from .set_reader import read_set, read_set_with_debug
from .answer_reader import read_answers, read_answers_with_debug
from .scorer import calculate_score, calculate_score_per_question

logger = logging.getLogger(__name__)


def process_sheet(image_path, template_path, answer_key=None, debug=False):
    """
    একটি OMR শিট সম্পূর্ণভাবে প্রসেস করো।

    এই function টি OMR processing pipeline এর সব ধাপ
    একক function call এ সম্পন্ন করে।

    Args:
        image_path (str | Path): OMR sheet image এর path
        template_path (str | Path): Template JSON file এর path
        answer_key (dict | None): Optional answer key for scoring
                                  যেমন {"1": "A", "2": "C", ...}
        debug (bool): Debug mode — extra info সহ return করবে

    Returns:
        dict: Processing result:
              - 'success': bool
              - 'image_path': str
              - 'student_numeric_id': str | None
              - 'exam_set': str | None  ('A'/'B'/'C'/'D')
              - 'answers': dict {q_num: answer}
              - 'score': dict (যদি answer_key দেওয়া হয়)
              - 'corners_found': bool
              - 'processing_time': float (seconds)
              - 'error': str (যদি error হয়)
              - 'debug_info': dict (যদি debug=True)
    """
    start_time = datetime.now()
    result = {
        'success': False,
        'image_path': str(image_path),
        'student_numeric_id': None,
        'exam_set': None,
        'answers': {},
        'score': None,
        'corners_found': False,
        'processing_time': 0.0,
        'error': None,
    }

    try:
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Step 1: Image Load
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        image_path = Path(image_path)
        if not image_path.exists():
            result['error'] = f'Image file not found: {image_path}'
            logger.error(result['error'])
            return result

        image = cv2.imread(str(image_path))
        if image is None:
            result['error'] = f'Failed to load image: {image_path}'
            logger.error(result['error'])
            return result

        logger.info(f"Image loaded: {image_path.name}, size={image.shape[1]}x{image.shape[0]}")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Step 2: Template Load
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        template_path = Path(template_path)
        if not template_path.exists():
            result['error'] = f'Template file not found: {template_path}'
            logger.error(result['error'])
            return result

        with open(template_path, 'r', encoding='utf-8') as f:
            template = json.load(f)

        logger.info(f"Template loaded: {template_path.name}")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Step 3: Perspective Correction
        # কোণার ■ চিহ্ন খুঁজে শিট সোজা করো
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        output_size = tuple(template.get('output_size', [794, 1123]))
        corners = find_corner_markers(image)

        if corners:
            warped = warp_sheet(image, corners, output_size=output_size)
            result['corners_found'] = True
            logger.info("Perspective correction successful")
        else:
            # Fallback: corner না পেলে resize করে চালিয়ে যাও
            warped = cv2.resize(image, output_size)
            logger.warning("Corner markers not found — using resize fallback")

        # Image preprocessing (optional — accuracy improve করতে)
        # warped = preprocess_image(warped)  # uncomment if needed

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Step 4: Data Extraction
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        debug_info = {}

        if debug:
            # Debug mode — সব bubble ratios সহ
            id_result = read_id_with_debug(warped, template)
            set_result = read_set_with_debug(warped, template)
            ans_result = read_answers_with_debug(warped, template)

            result['student_numeric_id'] = id_result['id']
            result['exam_set'] = set_result['set']
            result['answers'] = ans_result['answers']

            debug_info = {
                'id_debug': id_result,
                'set_debug': set_result,
                'answer_debug': ans_result['debug'],
            }
        else:
            # Normal mode
            result['student_numeric_id'] = read_id_number(warped, template)
            result['exam_set'] = read_set(warped, template)
            result['answers'] = read_answers(warped, template)

        logger.info(f"Student ID: {result['student_numeric_id']}, Set: {result['exam_set']}")
        logger.info(f"Answers extracted: {len(result['answers'])} questions")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Step 5: Score Calculation (যদি answer_key দেওয়া হয়)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if answer_key and result['answers']:
            scoring_config = template.get('scoring', {})
            correct_mark = scoring_config.get('correct_mark', 1.0)
            wrong_mark = scoring_config.get('wrong_mark', -0.25)

            result['score'] = calculate_score(
                result['answers'],
                answer_key,
                correct_mark=correct_mark,
                wrong_mark=wrong_mark,
            )

            if debug:
                debug_info['per_question'] = calculate_score_per_question(
                    result['answers'], answer_key,
                    correct_mark=correct_mark, wrong_mark=wrong_mark,
                )

            logger.info(f"Score: {result['score']['marks']} marks, "
                        f"{result['score']['correct']} correct, "
                        f"{result['score']['wrong']} wrong")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Step 6: Warped image (caller এর জন্য)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        result['warped_image'] = warped  # numpy array (in-memory)
        result['success'] = True

        if debug:
            result['debug_info'] = debug_info

    except json.JSONDecodeError as e:
        result['error'] = f'Template JSON parse error: {e}'
        logger.exception(result['error'])
    except cv2.error as e:
        result['error'] = f'OpenCV error: {e}'
        logger.exception(result['error'])
    except Exception as e:
        result['error'] = f'Unexpected error: {e}'
        logger.exception(result['error'])
    finally:
        # Processing time record করো
        elapsed = (datetime.now() - start_time).total_seconds()
        result['processing_time'] = round(elapsed, 3)
        logger.info(f"Processing completed in {elapsed:.3f}s")

    return result


def process_sheet_batch(image_paths, template_path, answer_key=None, debug=False):
    """
    একাধিক OMR শিট batch এ প্রসেস করো।

    Args:
        image_paths (list): Image file paths এর list
        template_path (str | Path): Template JSON file এর path
        answer_key (dict | None): Optional answer key
        debug (bool): Debug mode

    Returns:
        list: প্রতিটি শিটের processing result এর list
    """
    results = []
    total = len(image_paths)

    logger.info(f"Batch processing {total} sheets...")

    for idx, image_path in enumerate(image_paths, 1):
        logger.info(f"Processing {idx}/{total}: {image_path}")
        result = process_sheet(image_path, template_path, answer_key=answer_key, debug=debug)
        result['batch_index'] = idx
        results.append(result)

    success_count = sum(1 for r in results if r['success'])
    logger.info(f"Batch complete: {success_count}/{total} successful")

    return results


def save_debug_image(warped_image, output_path, template=None):
    """
    Debug purpose: Warped image এ template grid overlay করে save করো।

    Args:
        warped_image (np.ndarray): Warped OMR sheet image
        output_path (str | Path): Output image save path
        template (dict | None): Template config (grid drawing এর জন্য)
    """
    debug_img = warped_image.copy()

    if template:
        # ID Grid box আঁকো
        if 'id_grid' in template:
            reg = template['id_grid']
            x, y, w, h = int(reg['x']), int(reg['y']), int(reg['w']), int(reg['h'])
            cv2.rectangle(debug_img, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.putText(debug_img, 'ID Grid', (x, y - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Set Region box আঁকো
        if 'set_region' in template:
            reg = template['set_region']
            x, y, w, h = int(reg['x']), int(reg['y']), int(reg['w']), int(reg['h'])
            cv2.rectangle(debug_img, (x, y), (x + w, y + h), (255, 0, 0), 2)
            cv2.putText(debug_img, 'Set', (x, y - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

        # Answer Columns box আঁকো
        for col_info in template.get('answer_columns', []):
            x, y, w, h = int(col_info['x']), int(col_info['y']), int(col_info['w']), int(col_info['h'])
            q_start = col_info.get('q_start', '?')
            q_end = col_info.get('q_end', '?')
            cv2.rectangle(debug_img, (x, y), (x + w, y + h), (0, 0, 255), 2)
            cv2.putText(debug_img, f'Q{q_start}-{q_end}', (x, y - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    cv2.imwrite(str(output_path), debug_img)
    logger.info(f"Debug image saved: {output_path}")
