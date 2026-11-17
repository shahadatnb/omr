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
    OMR শিটের ৪টি কোণা চিহ্নিত করো।
    
    ১. যদি শিটে ৪টি কোণার কালো ■ মার্কার থাকে (যেমন DU B-Unit বা স্ট্যান্ডার্ড ওএমআর),
       তবে ৪টি কোয়ারড্রেন্টে অবস্থিত দূরবর্তী মার্কারগুলো শনাক্ত করে।
    ২. যদি শিটে কোনো কোণার মার্কার না থাকে (যেমন অ্যাডমিশন চ্যালেঞ্জ ইত্যাদি),
       তবে ব্যাকগ্রাউন্ডের বিপরীতে কাগজের আয়তাকার বাউন্ডারি (Paper Contour) শনাক্ত করে।
    ৩. যদি কাগজ ক্যামেরা ফ্রেমজুড়ে পূর্ণ থাকে, তবে সম্পূর্ণ ফ্রেম রিটার্ন করে।
    
    Returns:
        list: 4টি corner points [(x,y), ...] যেমন [TL, TR, BL, BR]
              অথবা None
    """
    if image is None or image.size == 0:
        return None

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()
    h, w = gray.shape[:2]
    img_area = float(h * w)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Step 1: True Corner Square Markers (■) Detection
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # কোণার মার্কার হতে হলে অবশ্যই ৪টি ভিন্ন কোয়ারড্রেন্টে (TL, TR, BL, BR)
    # অবস্থিত হতে হবে এবং সমগ্র কাগজের অন্তত ৫০% বিস্তার থাকতে হবে।
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    for t_val in [60, 80, 100, 120, 140]:
        _, thresh = cv2.threshold(blurred, t_val, 255, cv2.THRESH_BINARY_INV)
        cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        squares = []
        for c in cnts:
            area = cv2.contourArea(c)
            # Corner marker must be reasonably sized (0.02% to 3% of sheet)
            if not (img_area * 0.0002 < area < img_area * 0.03):
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.05 * peri, True)
            if len(approx) in [4, 5, 6]:
                x, y, bw, bh = cv2.boundingRect(c)
                ar = bw / float(bh) if bh > 0 else 0
                if 0.65 <= ar <= 1.45:
                    squares.append((int(x + bw / 2), int(y + bh / 2)))

        if len(squares) >= 4:
            mid_x, mid_y = w / 2, h / 2
            tl = [p for p in squares if p[0] < mid_x and p[1] < mid_y]
            tr = [p for p in squares if p[0] >= mid_x and p[1] < mid_y]
            bl = [p for p in squares if p[0] < mid_x and p[1] >= mid_y]
            br = [p for p in squares if p[0] >= mid_x and p[1] >= mid_y]

            # All 4 quadrants MUST contain at least one candidate marker
            if tl and tr and bl and br:
                c_tl = min(tl, key=lambda p: p[0]**2 + p[1]**2)
                c_tr = min(tr, key=lambda p: (w - p[0])**2 + p[1]**2)
                c_bl = min(bl, key=lambda p: p[0]**2 + (h - p[1])**2)
                c_br = min(br, key=lambda p: (w - p[0])**2 + (h - p[1])**2)

                w_span = max(c_tr[0], c_br[0]) - min(c_tl[0], c_bl[0])
                h_span = max(c_bl[1], c_br[1]) - min(c_tl[1], c_tr[1])

                # Must span across at least 50% width and 50% height
                if w_span > 0.50 * w and h_span > 0.50 * h:
                    logger.info("True 4-corner square markers detected successfully")
                    return _sort_corners([c_tl, c_tr, c_bl, c_br])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Step 2: Paper Sheet Boundary Detection (for sheets without ■)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    sheet_quad = _find_sheet_outer_contour(gray, img_area)
    if sheet_quad:
        logger.info("Sheet paper contour detected successfully")
        return _sort_corners(sheet_quad)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Step 3: Full Frame Fallback
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    logger.info("Using full frame boundaries for sheet")
    return [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]


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
    ব্যাকগ্রাউন্ডের বিপরীতে কাগজের শিটের আয়তাকার বাউন্ডারি (Paper Contour) শনাক্ত করে।
    মোবাইল ক্যামেরার ছবি, টেবিল বা ডেস্কের পটভূমি থেকে সাদা কাগজের ৪টি কোণা নির্ভুলভাবে বের করে।
    """
    h, w = gray.shape[:2]
    smooth = cv2.bilateralFilter(gray, 9, 75, 75)

    best_quad = None
    best_area = 0

    # 1. Otsu thresholding across multiple factors
    otsu_val, _ = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for factor in [1.0, 0.85, 1.15, 0.70]:
        t_val = int(min(245, max(40, otsu_val * factor)))
        _, binary = cv2.threshold(smooth, t_val, 255, cv2.THRESH_BINARY)
        cnts, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        for c in cnts:
            area = cv2.contourArea(c)
            if area < img_area * 0.25 or area > img_area * 0.98:
                continue
            peri = cv2.arcLength(c, True)
            for eps in [0.02, 0.03, 0.04, 0.05]:
                approx = cv2.approxPolyDP(c, eps * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    if area > best_area:
                        best_area = area
                        best_quad = [tuple(int(v) for v in p[0]) for p in approx]

    if best_quad:
        return best_quad

    # 2. Canny edge fallback
    for canny_low, canny_high in [(30, 100), (20, 80), (50, 150)]:
        edges = cv2.Canny(smooth, canny_low, canny_high)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        dilated = cv2.dilate(edges, kernel, iterations=2)
        cnts, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            area = cv2.contourArea(c)
            if area < img_area * 0.25 or area > img_area * 0.98:
                continue
            peri = cv2.arcLength(c, True)
            for eps in [0.02, 0.03, 0.04, 0.05]:
                approx = cv2.approxPolyDP(c, eps * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    if area > best_area:
                        best_area = area
                        best_quad = [tuple(int(v) for v in p[0]) for p in approx]

    return best_quad


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
