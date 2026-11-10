"""
omr_engine/set_reader.py
Exam Set (A/B/C/D) reading module.

এই মডিউলটি OMR শিটের উপরে ডান দিকে থাকা
Set bubble থেকে পরীক্ষার Set (A, B, C, বা D) পড়ে।

Set Layout:
- ৪টি bubble (A, B, C, D) উপর থেকে নিচে সাজানো
- শিক্ষার্থী তার exam set বাছাই করে fill করে
"""

import logging
from .roi_extractor import extract_roi, split_grid_with_padding
from .bubble_detector import get_filled_index, get_all_fill_ratios

logger = logging.getLogger(__name__)

# Set options — index থেকে letter mapping
SET_OPTIONS = ['A', 'B', 'C', 'D']


def read_set(warped_image, template, padding=2):
    """
    OMR শিট থেকে Exam Set (A/B/C/D) পড়ো।

    Set Region Layout:
    - ৪টি bubble (rows=4, cols=1)
    - প্রথম bubble (index 0) = Set A
    - দ্বিতীয় bubble (index 1) = Set B
    - তৃতীয় bubble (index 2) = Set C
    - চতুর্থ bubble (index 3) = Set D

    Args:
        warped_image (np.ndarray): Perspective-corrected OMR sheet image
        template (dict): Template configuration with 'set_region' key
        padding (int): Cell edge padding in pixels

    Returns:
        str | None: 'A', 'B', 'C', বা 'D'
                   অথবা None যদি কোনো bubble fill না হয়
    """
    set_config = template.get('set_region')
    if set_config is None:
        logger.error("Template 'set_region' config missing")
        return None

    # Set Region ROI extract করো
    set_roi = extract_roi(warped_image, set_config)
    if set_roi.size == 0:
        logger.error("Failed to extract set region ROI")
        return None

    # Grid ভাগ করো: 4 rows (A,B,C,D), 1 column
    cells = split_grid_with_padding(set_roi, rows=4, cols=1, padding=padding)

    # প্রতিটি row এর single bubble নাও
    bubbles = [cells[i][0] for i in range(4)]

    # কোন bubble টি filled?
    filled_idx = get_filled_index(bubbles)

    if filled_idx is None:
        logger.warning("No set bubble filled — cannot determine exam set")
        return None

    if filled_idx >= len(SET_OPTIONS):
        logger.error(f"Invalid set index: {filled_idx}")
        return None

    exam_set = SET_OPTIONS[filled_idx]
    logger.info(f"Exam Set detected: {exam_set}")
    return exam_set


def read_set_with_debug(warped_image, template):
    """
    Debug mode এ Set পড়ো — সব bubble এর fill ratios সহ।

    Args:
        warped_image (np.ndarray): Warped OMR sheet image
        template (dict): Template configuration

    Returns:
        dict: {'set': str|None, 'fill_ratios': list}
    """
    set_config = template.get('set_region')
    if set_config is None:
        return {'set': None, 'fill_ratios': []}

    set_roi = extract_roi(warped_image, set_config)
    if set_roi.size == 0:
        return {'set': None, 'fill_ratios': []}

    cells = split_grid_with_padding(set_roi, rows=4, cols=1, padding=2)
    bubbles = [cells[i][0] for i in range(4)]

    ratios = get_all_fill_ratios(bubbles)
    filled_idx = get_filled_index(bubbles)

    return {
        'set': SET_OPTIONS[filled_idx] if filled_idx is not None else None,
        'fill_ratios': {SET_OPTIONS[i]: round(r, 4) for i, r in enumerate(ratios)}
    }
