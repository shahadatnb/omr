"""
omr_engine/id_reader.py
Student ID number reading module.

এই মডিউলটি OMR শিটের ID Grid থেকে ৬ ডিজিটের
রেজিস্ট্রেশন নম্বর পড়ে।

ID Grid Layout:
- ৬টি কলাম (প্রতিটি digit position এর জন্য)
- ১০টি সারি (০ থেকে ৯ পর্যন্ত)
- শিক্ষার্থী নিজের ৬ ডিজিটের ID এর প্রতিটি digit
  সংশ্লিষ্ট কলামের সেই সারিতে fill করে
"""

import logging
from .roi_extractor import extract_roi, split_grid_with_padding
from .bubble_detector import get_filled_index, get_all_fill_ratios

logger = logging.getLogger(__name__)


def read_id_number(warped_image, template, padding=2):
    """
    OMR শিট থেকে ৬ ডিজিটের রেজিস্ট্রেশন নম্বর পড়ো।

    Grid Layout (794×1123 coordinate system):
    - প্রতিটি কলাম = একটি digit position (1st, 2nd, ... 6th)
    - প্রতিটি সারি = সেই position এর digit value (0, 1, 2, ..., 9)
    - উদাহরণ: ID=269003 → col0=row2, col1=row6, col2=row9,
                            col3=row0, col4=row0, col5=row3

    Args:
        warped_image (np.ndarray): Perspective-corrected OMR sheet image
        template (dict): Template configuration with 'id_grid' key
        padding (int): Cell edge padding for better accuracy

    Returns:
        str | None: ৬ ডিজিটের ID string যেমন '269003'
                   অথবা None যদি কোনো digit পড়া না যায়
    """
    id_config = template.get('id_grid')
    if id_config is None:
        logger.error("Template 'id_grid' config missing")
        return None

    # ID Grid ROI extract করো
    id_roi = extract_roi(warped_image, id_config)
    if id_roi.size == 0:
        logger.error("Failed to extract ID grid ROI")
        return None

    # Grid ভাগ করো:
    # rows=10 (digit 0-9), cols=6 (digit position 1-6)
    num_cols = id_config.get('columns', 6)   # digit positions
    num_rows = id_config.get('rows', 10)     # digit values (0-9)

    cells = split_grid_with_padding(id_roi, rows=num_rows, cols=num_cols, padding=padding)

    digits = []
    for col in range(num_cols):
        # এই column এর সব row এর bubbles নাও
        col_bubbles = [cells[row][col] for row in range(num_rows)]

        # কোন row টি filled?
        filled_idx = get_filled_index(col_bubbles)

        if filled_idx is None:
            logger.warning(f"ID digit position {col + 1}: No bubble filled")
            digits.append('?')
        else:
            digit = str(filled_idx)  # row index = digit value (row 0 → '0', row 5 → '5')
            digits.append(digit)
            logger.debug(f"ID digit position {col + 1}: {digit}")

    id_string = ''.join(digits)
    logger.info(f"Read ID: {id_string}")

    # যদি কোনো digit অপঠনযোগ্য হয়
    if '?' in id_string:
        logger.warning(f"Incomplete ID read: {id_string}")
        return None

    return id_string


def read_id_with_debug(warped_image, template):
    """
    Debug mode এ ID পড়ো — সব bubble এর fill ratios সহ।

    Args:
        warped_image (np.ndarray): Warped OMR sheet image
        template (dict): Template configuration

    Returns:
        dict: {'id': str, 'debug_ratios': list of list}
    """
    id_config = template.get('id_grid')
    if id_config is None:
        return {'id': None, 'debug_ratios': []}

    id_roi = extract_roi(warped_image, id_config)
    if id_roi.size == 0:
        return {'id': None, 'debug_ratios': []}

    num_cols = id_config.get('columns', 6)
    num_rows = id_config.get('rows', 10)

    cells = split_grid_with_padding(id_roi, rows=num_rows, cols=num_cols, padding=2)

    digits = []
    all_ratios = []

    for col in range(num_cols):
        col_bubbles = [cells[row][col] for row in range(num_rows)]
        ratios = get_all_fill_ratios(col_bubbles)
        all_ratios.append(ratios)

        filled_idx = get_filled_index(col_bubbles)
        digits.append(str(filled_idx) if filled_idx is not None else '?')

    id_string = ''.join(digits)
    return {
        'id': None if '?' in id_string else id_string,
        'digits': digits,
        'debug_ratios': all_ratios
    }
