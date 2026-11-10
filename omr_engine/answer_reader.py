"""
omr_engine/answer_reader.py
MCQ Answer reading module for 100-question OMR sheet.

এই মডিউলটি OMR শিটের ৪টি column থেকে ১০০ প্রশ্নের
MCQ উত্তর পড়ে।

Answer Grid Layout:
- ৪টি column:
  * Column 1: Q1-Q25
  * Column 2: Q26-Q50
  * Column 3: Q51-Q75
  * Column 4: Q76-Q100
- প্রতিটি প্রশ্নে ৪টি option: A, B, C, D (বাম থেকে ডানে)
"""

import logging
from .roi_extractor import extract_roi, split_grid_with_padding
from .bubble_detector import get_filled_index, get_all_fill_ratios

logger = logging.getLogger(__name__)

# Answer options — index থেকে letter mapping
ANSWER_OPTIONS = ['A', 'B', 'C', 'D']


def read_answers(warped_image, template, padding=2):
    """
    OMR শিট থেকে ১০০ প্রশ্নের MCQ উত্তর পড়ো।

    Template 'answer_columns' এ ৪টি column এর config থাকে:
    প্রতিটি column:
    - 'q_start': প্রথম প্রশ্ন নম্বর
    - 'q_end': শেষ প্রশ্ন নম্বর
    - 'x', 'y', 'w', 'h': ROI coordinates

    Args:
        warped_image (np.ndarray): Perspective-corrected OMR sheet image
        template (dict): Template configuration with 'answer_columns' key
        padding (int): Cell edge padding in pixels

    Returns:
        dict: {question_number: answer_letter}
              যেমন {1: 'A', 2: 'C', 3: None, 4: 'B', ...}
              None মানে উত্তর দেওয়া হয়নি
    """
    answer_columns = template.get('answer_columns', [])
    if not answer_columns:
        logger.error("Template 'answer_columns' config missing or empty")
        return {}

    answers = {}

    for col_idx, col_info in enumerate(answer_columns):
        q_start = col_info.get('q_start', 1)
        q_end = col_info.get('q_end', 25)
        num_questions = q_end - q_start + 1

        logger.debug(f"Processing answer column {col_idx + 1}: Q{q_start}-Q{q_end}")

        # এই column এর ROI extract করো
        col_roi = extract_roi(warped_image, col_info)
        if col_roi.size == 0:
            logger.error(f"Failed to extract answer column {col_idx + 1} ROI")
            # সব প্রশ্ন None দিয়ে ভরো
            for q_num in range(q_start, q_end + 1):
                answers[q_num] = None
            continue

        # Grid ভাগ করো:
        # rows = question সংখ্যা (25), cols = 4 options (A,B,C,D)
        cells = split_grid_with_padding(col_roi, rows=num_questions, cols=4, padding=padding)

        # প্রতিটি প্রশ্নের উত্তর বের করো
        for row_idx in range(num_questions):
            q_num = q_start + row_idx

            # এই প্রশ্নের ৪টি option bubble নাও (A, B, C, D)
            row_bubbles = [cells[row_idx][c] for c in range(4)]

            # কোন bubble টি filled?
            filled_idx = get_filled_index(row_bubbles)

            if filled_idx is None:
                answers[q_num] = None
                logger.debug(f"Q{q_num}: No answer (unattempted)")
            elif filled_idx >= len(ANSWER_OPTIONS):
                answers[q_num] = None
                logger.error(f"Q{q_num}: Invalid filled index {filled_idx}")
            else:
                answers[q_num] = ANSWER_OPTIONS[filled_idx]
                logger.debug(f"Q{q_num}: {answers[q_num]}")

    logger.info(f"Total answers read: {len(answers)}, "
                f"Attempted: {sum(1 for v in answers.values() if v is not None)}, "
                f"Unattempted: {sum(1 for v in answers.values() if v is None)}")

    return answers


def read_answers_with_debug(warped_image, template):
    """
    Debug mode এ উত্তর পড়ো — সব bubble এর fill ratios সহ।

    Args:
        warped_image (np.ndarray): Warped OMR sheet image
        template (dict): Template configuration

    Returns:
        dict: {'answers': dict, 'debug': {q_num: {option: ratio}}}
    """
    answer_columns = template.get('answer_columns', [])
    answers = {}
    debug_info = {}

    for col_info in answer_columns:
        q_start = col_info.get('q_start', 1)
        q_end = col_info.get('q_end', 25)
        num_questions = q_end - q_start + 1

        col_roi = extract_roi(warped_image, col_info)
        if col_roi.size == 0:
            continue

        cells = split_grid_with_padding(col_roi, rows=num_questions, cols=4, padding=2)

        for row_idx in range(num_questions):
            q_num = q_start + row_idx
            row_bubbles = [cells[row_idx][c] for c in range(4)]

            ratios = get_all_fill_ratios(row_bubbles)
            filled_idx = get_filled_index(row_bubbles)

            answers[q_num] = ANSWER_OPTIONS[filled_idx] if filled_idx is not None else None
            debug_info[q_num] = {
                ANSWER_OPTIONS[i]: round(r, 4) for i, r in enumerate(ratios)
            }

    return {'answers': answers, 'debug': debug_info}
