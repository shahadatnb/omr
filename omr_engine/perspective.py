"""
omr_engine/perspective.py
Perspective correction module for OMR sheet processing.

এই মডিউলটি OMR শিটের ৪টি কোণার কালো ■ চিহ্ন খুঁজে বের করে
perspective transform দিয়ে শিটটিকে সোজা করে।
মোবাইল ফোনের ক্যামেরা, পরিবর্তিত আলোর উজ্জ্বলতা (Lighting/Shadow),
এবং বিভিন্ন রেজোলিউশনের জন্য মাল্টি-থ্রেশহোল্ড ও অ্যাডাপটিভ কর্নার ডিটেকশন সমর্থন করে।
"""

import cv2
import numpy as np
import logging

logger = logging.getLogger(__name__)


def find_corner_markers(image, min_area=None, max_area=None, aspect_tolerance=0.45):
    """
    OMR শিটের ৪টি কোণার কালো ■ চিহ্ন খুঁজে বের করো।

    Algorithm:
    1. Grayscale + Multi-thresholding (Otsu + dynamic scale) দিয়ে dark squares খোঁজো
    2. Dynamic area scaling (ইমেজের মোট রেজোলিউশনের সাথে স্কেল করা)
    3. ৪টি কোণার সবচেয়ে উপযুক্ত চতুর্ভুজ (Quad) নির্বাচন করো
    4. ফলব্যাক: শিটের ৪-কোণা বাউন্ডারি কনট্যুর (যদি কোণার মার্কার অস্পষ্ট থাকে)
    5. TL, TR, BL, BR অনুযায়ী sort করো

    Args:
        image (np.ndarray): BGR image (OpenCV format)
        min_area (int | None): কোণার চিহ্নের সর্বনিম্ন area (None হলে স্বয়ংক্রিয় স্কেল হবে)
        max_area (int | None): কোণার চিহ্নের সর্বোচ্চ area (None হলে স্বয়ংক্রিয় স্কেল হবে)
        aspect_tolerance (float): Width/Height ratio tolerance (0.45 = perspective tilt সহনশীল)

    Returns:
        list: 4টি corner points [(x,y), ...] যেমন [TL, TR, BL, BR]
              অথবা None যদি ৪টি corner না পাওয়া যায়
    """
    if image is None or image.size == 0:
        return None

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()
    h, w = gray.shape[:2]
    img_area = float(h * w)

    # Dynamic scaling based on image resolution
    # On standard OMR sheets, a corner square is ~0.03% to 0.5% of total sheet area
    if min_area is None or min_area <= 0:
        min_area = max(50, int(img_area * 0.00004))
    if max_area is None or max_area <= 0:
        max_area = max(1000, int(img_area * 0.025))

    # Multi-threshold strategies to handle shadows, direct sunlight, and dim rooms
    otsu_val, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    otsu_int = int(otsu_val)
    thresholds_to_try = [otsu_int, int(otsu_int * 0.8), int(otsu_int * 1.2), 60, 80, 100, 120]

    seen = set()
    cleaned_thresh = []
    for t in thresholds_to_try:
        t = max(30, min(220, t))
        if t not in seen:
            seen.add(t)
            cleaned_thresh.append(t)

    best_corners = None
    max_quad_area = 0

    kernel = np.ones((3, 3), np.uint8)

    for t_val in cleaned_thresh:
        _, thresh = cv2.threshold(gray, t_val, 255, cv2.THRESH_BINARY_INV)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        squares = []
        for c in contours:
            area = cv2.contourArea(c)
            if not (min_area < area < max_area):
                continue

            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.06 * peri, True)

            # 4 or 5 vertices (in case of slight camera blur or rounding)
            if len(approx) in (4, 5, 6):
                x, y, bw, bh = cv2.boundingRect(approx)
                aspect = bw / float(bh) if bh > 0 else 0
                if abs(1.0 - aspect) < aspect_tolerance:
                    cx = int(x + bw // 2)
                    cy = int(y + bh // 2)
                    squares.append((cx, cy))

        if len(squares) >= 4:
            selected = _select_best_corners(squares, (h, w))
            if selected and len(selected) == 4:
                sorted_c = _sort_corners(selected)
                qw = max(abs(sorted_c[1][0] - sorted_c[0][0]), abs(sorted_c[3][0] - sorted_c[2][0]))
                qh = max(abs(sorted_c[2][1] - sorted_c[0][1]), abs(sorted_c[3][1] - sorted_c[1][1]))
                quad_area = float(qw * qh)

                if quad_area > img_area * 0.15:
                    if quad_area > max_quad_area:
                        max_quad_area = quad_area
                        best_corners = sorted_c
                    # If already spans over 35% of the frame, this is an optimal match
                    if quad_area > img_area * 0.35:
                        logger.info(f"Corner markers cleanly found with thresh={t_val}")
                        return best_corners

    if best_corners:
        logger.info(f"Corner markers selected (quad_area={max_quad_area:.0f})")
        return best_corners

    # Fallback: Sheet boundary contour detection
    sheet_quad = _find_sheet_outer_contour(gray, img_area)
    if sheet_quad:
        logger.info("Found sheet boundary contour as perspective fallback")
        return _sort_corners(sheet_quad)

    logger.warning("No corner markers or sheet contour could be reliably identified")
    return None


def _select_best_corners(candidates, image_shape):
    """
    একাধিক candidate থেকে সবচেয়ে দূরবর্তী ৪টি corner বেছে নাও
    যা শিটের সম্পূর্ণ পরিধি (Convex Hull Bounding Box) ধারণ করে।
    """
    pts = np.array(candidates)
    if len(pts) == 4:
        return [tuple(int(v) for v in p) for p in pts]

    s = pts[:, 0] + pts[:, 1]
    diff = pts[:, 0] - pts[:, 1]

    tl_idx = int(np.argmin(s))
    br_idx = int(np.argmax(s))
    tr_idx = int(np.argmax(diff))
    bl_idx = int(np.argmin(diff))

    selected_indices = [tl_idx, tr_idx, bl_idx, br_idx]
    if len(set(selected_indices)) == 4:
        return [tuple(int(v) for v in pts[i]) for i in selected_indices]

    # Fallback: distance to ideal bounding box of candidates
    min_x, max_x = float(np.min(pts[:, 0])), float(np.max(pts[:, 0]))
    min_y, max_y = float(np.min(pts[:, 1])), float(np.max(pts[:, 1]))
    ideal = [(min_x, min_y), (max_x, min_y), (min_x, max_y), (max_x, max_y)]

    selected = []
    used = set()
    for ix, iy in ideal:
        dists = (pts[:, 0] - ix)**2 + (pts[:, 1] - iy)**2
        sorted_indices = np.argsort(dists)
        for idx in sorted_indices:
            idx = int(idx)
            if idx not in used:
                selected.append(tuple(int(v) for v in pts[idx]))
                used.add(idx)
                break

    return selected


def _find_sheet_outer_contour(gray, img_area):
    """
    ফলব্যাক মেথড: ব্যাকগ্রাউন্ডের বিপরীতে কাগজের শিটের আয়তাকার বাউন্ডারি শনাক্ত করে।
    """
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 30, 120)
    kernel = np.ones((5, 5), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    for c in contours[:8]:
        area = cv2.contourArea(c)
        if area < img_area * 0.20:
            break
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.03 * peri, True)
        if len(approx) == 4:
            pts = [tuple(int(v) for v in p[0]) for p in approx]
            return pts
    return None


def _sort_corners(corners):
    """
    ৪টি corner কে TL, TR, BL, BR ক্রমে সাজাও।

    Args:
        corners (list): 4টি (x, y) points

    Returns:
        list: [TL, TR, BL, BR] ক্রমে sorted corners
    """
    # Y অনুযায়ী sort করে top 2 এবং bottom 2 আলাদা করো
    corners_sorted_y = sorted(corners, key=lambda p: p[1])
    top = sorted(corners_sorted_y[:2], key=lambda p: p[0])      # X অনুযায়ী → TL, TR
    bottom = sorted(corners_sorted_y[-2:], key=lambda p: p[0])  # X অনুযায়ী → BL, BR

    tl, tr = top
    bl, br = bottom
    return [tl, tr, bl, br]


def warp_sheet(image, corners, output_size=(794, 1123)):
    """
    Perspective transform দিয়ে OMR শিট সোজা করো।

    Camera angle বা placement এর কারণে শিট বাঁকা থাকতে পারে।
    এই function টি ৪টি corner point ব্যবহার করে শিটকে
    সঠিক টেমপ্লেট অনুপাতে রূপান্তর করে।

    Args:
        image (np.ndarray): Input BGR image
        corners (list): [TL, TR, BL, BR] corner points
        output_size (tuple): Output image size (width, height)
                            Default: 794×1123 (A4 at 96 DPI)

    Returns:
        np.ndarray: Perspective-corrected warped image
    """
    tl, tr, bl, br = corners

    # Source points — original image এর corner positions
    pts_src = np.float32([tl, tr, bl, br])

    # Destination points — output image এর corner positions
    w, h = output_size
    pts_dst = np.float32([
        [0, 0],       # TL
        [w, 0],       # TR
        [0, h],       # BL
        [w, h]        # BR
    ])

    # Perspective transformation matrix হিসাব করো
    M = cv2.getPerspectiveTransform(pts_src, pts_dst)

    # Transformation apply করো
    warped = cv2.warpPerspective(image, M, (w, h))

    logger.info(f"Perspective correction applied. Output size: {w}x{h}")
    return warped


def preprocess_image(image):
    """
    Image preprocessing — noise reduction এবং contrast enhancement।
    """
    blurred = cv2.GaussianBlur(image, (3, 3), 0)
    gray = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    result = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    return result
