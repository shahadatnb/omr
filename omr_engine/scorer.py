"""
omr_engine/scorer.py
Score calculation module with negative marking.

এই মডিউলটি MCQ উত্তরের সাথে answer key মিলিয়ে
নেগেটিভ মার্কিং সহ চূড়ান্ত স্কোর হিসাব করে।

Scoring Rules (Admission Challenge):
- সঠিক উত্তর: +1.0 (default)
- ভুল উত্তর: -0.25 (default, negative marking)
- উত্তর না দিলে: 0
- মোট marks ০ এর নিচে যেতে পারবে না
"""

import logging

logger = logging.getLogger(__name__)


def calculate_score(student_answers, answer_key, correct_mark=1.0, wrong_mark=-0.25, subjects=None):
    """
    নেগেটিভ মার্কিং সহ MCQ স্কোর হিসাব করো।
    যদি subjects তালিকা দেওয়া থাকে, তবে বিষয়ভিত্তিক নম্বর ও সঠিক/ভুলও আলাদাভাবে হিসাব করা হবে।

    Args:
        student_answers (dict): শিক্ষার্থীর উত্তর
                                যেমন {1: 'A', 2: None, 3: 'C', ...}
                                None মানে উত্তর দেওয়া হয়নি
        answer_key (dict): সঠিক উত্তরের চাবি
                          যেমন {"1": "C", "2": "B", "3": "A", ...}
                          Note: keys string হতে পারে (JSON format)
        correct_mark (float): সঠিক উত্তরে প্রাপ্ত marks (default: 1.0)
        wrong_mark (float): ভুল উত্তরে কাটা marks (default: -0.25)
                           Negative value দিতে হবে
        subjects (list | None): বিষয়সমূহের তালিকা
                               [{"name": "Bangla", "q_start": 1, "q_end": 15}, ...]

    Returns:
        dict: স্কোর summary:
              - 'correct': সঠিক উত্তরের সংখ্যা
              - 'wrong': ভুল উত্তরের সংখ্যা
              - 'unattempted': উত্তর না দেওয়া প্রশ্নের সংখ্যা
              - 'marks': মোট প্রাপ্ত marks (float)
              - 'total_questions': মোট প্রশ্ন সংখ্যা
              - 'percentage': শতকরা নম্বর
              - 'subject_marks': {'Bangla': 12.5, 'English': 10.0} (যদি subjects থাকে)
              - 'subject_details': বিষয়ভিত্তিক বিস্তারিত (যদি subjects থাকে)
    """
    correct = 0
    wrong = 0
    unattempted = 0
    skipped = 0  # answer key তে নেই এমন প্রশ্ন

    total_questions = len(student_answers)

    for q_num, student_ans in student_answers.items():
        # Answer key এ এই প্রশ্ন আছে কিনা চেক করো
        # Note: answer key এ key string হতে পারে (JSON থেকে এলে)
        correct_ans = answer_key.get(str(q_num)) or answer_key.get(int(q_num))

        if correct_ans is None:
            skipped += 1
            logger.debug(f"Q{q_num}: Not in answer key, skipping")
            continue

        c_upper = str(correct_ans).strip().upper()
        has_student_ans = student_ans is not None and str(student_ans).strip() != ''

        if c_upper == 'ANY':
            # যেকোনোটা ফিল করলেই আনসার সঠিক হবে
            if has_student_ans:
                correct += 1
                logger.debug(f"Q{q_num}: Correct (ANY filled, student={student_ans})")
            else:
                unattempted += 1
                logger.debug(f"Q{q_num}: Unattempted (ANY not filled)")
        elif c_upper == 'NONE':
            # কোনোটা ফিল না করলেই আনসার সঠিক হবে
            if not has_student_ans:
                correct += 1
                logger.debug(f"Q{q_num}: Correct (NONE - left blank)")
            else:
                wrong += 1
                logger.debug(f"Q{q_num}: Wrong (NONE expected but student filled {student_ans})")
        else:
            # উত্তরের সাধারণ মান নির্ধারণ করো
            if not has_student_ans:
                unattempted += 1
                logger.debug(f"Q{q_num}: Unattempted")
            elif str(student_ans).strip().upper() == c_upper:
                correct += 1
                logger.debug(f"Q{q_num}: Correct ({student_ans})")
            else:
                wrong += 1
                logger.debug(f"Q{q_num}: Wrong (student={student_ans}, correct={correct_ans})")

    # বিষয়ভিত্তিক নম্বর ও ফলাফল হিসাব
    subject_marks = {}
    subject_details = {}

    if subjects and isinstance(subjects, list):
        for sub in subjects:
            s_name = sub.get('name', 'Unknown')
            q_s = int(sub.get('q_start', 1))
            q_e = int(sub.get('q_end', q_s))

            s_correct = 0
            s_wrong = 0
            s_unattempted = 0
            s_total_q = 0

            for q_idx in range(q_s, q_e + 1):
                s_total_q += 1
                c_ans = answer_key.get(str(q_idx)) or answer_key.get(int(q_idx))
                s_ans = student_answers.get(q_idx) or student_answers.get(str(q_idx))
                has_s_ans = s_ans is not None and str(s_ans).strip() != ''

                if c_ans is None:
                    continue

                c_u = str(c_ans).strip().upper()
                if c_u == 'ANY':
                    if has_s_ans:
                        s_correct += 1
                    else:
                        s_unattempted += 1
                elif c_u == 'NONE':
                    if not has_s_ans:
                        s_correct += 1
                    else:
                        s_wrong += 1
                elif not has_s_ans:
                    s_unattempted += 1
                elif str(s_ans).strip().upper() == c_u:
                    s_correct += 1
                else:
                    s_wrong += 1

            s_raw = (s_correct * correct_mark) + (s_wrong * wrong_mark)
            s_marks = max(0.0, round(s_raw, 2))
            subject_marks[s_name] = s_marks
            subject_details[s_name] = {
                'marks': s_marks,
                'raw_marks': round(s_raw, 2),
                'correct': s_correct,
                'wrong': s_wrong,
                'unattempted': s_unattempted,
                'total_questions': s_total_q
            }

    # মোট marks হিসাব করো
    raw_marks = (correct * correct_mark) + (wrong * wrong_mark)

    # মোট marks ০ এর নিচে যেতে পারবে না
    marks = max(0.0, round(raw_marks, 2))

    # Total marks (answer key তে যত প্রশ্ন)
    total_marked = total_questions - skipped
    max_marks = total_marked * correct_mark if total_marked > 0 else 1

    # Percentage হিসাব করো
    percentage = round((marks / max_marks) * 100, 2) if max_marks > 0 else 0.0

    result = {
        'correct': correct,
        'wrong': wrong,
        'unattempted': unattempted,
        'skipped_in_key': skipped,
        'marks': marks,
        'raw_marks': round(raw_marks, 2),  # নেগেটিভ হওয়ার আগের marks
        'total_questions': total_questions,
        'max_marks': max_marks,
        'percentage': percentage,
        'subject_marks': subject_marks if subjects else None,
        'subject_details': subject_details if subjects else None,
    }

    logger.info(f"Score: {marks}/{max_marks} ({percentage}%) | "
                f"Correct={correct}, Wrong={wrong}, Unattempted={unattempted}")

    return result


def calculate_score_per_question(student_answers, answer_key, correct_mark=1.0, wrong_mark=-0.25):
    """
    প্রতিটি প্রশ্নের জন্য বিস্তারিত স্কোর তৈরি করো।

    Args:
        student_answers (dict): শিক্ষার্থীর উত্তর {q_num: answer}
        answer_key (dict): Answer key {"q_num": "answer"}
        correct_mark (float): Correct answer mark
        wrong_mark (float): Wrong answer mark (negative)

    Returns:
        dict: প্রতিটি প্রশ্নের বিস্তারিত:
              {q_num: {'student': answer, 'correct': answer, 'status': status, 'mark': float}}
    """
    per_question = {}

    for q_num, student_ans in student_answers.items():
        correct_ans = answer_key.get(str(q_num)) or answer_key.get(int(q_num))

        if correct_ans is None:
            status = 'not_in_key'
            mark = 0.0
        else:
            c_upper = str(correct_ans).strip().upper()
            has_student_ans = student_ans is not None and str(student_ans).strip() != ''

            if c_upper == 'ANY':
                if has_student_ans:
                    status = 'correct'
                    mark = correct_mark
                else:
                    status = 'unattempted'
                    mark = 0.0
            elif c_upper == 'NONE':
                if not has_student_ans:
                    status = 'correct'
                    mark = correct_mark
                else:
                    status = 'wrong'
                    mark = wrong_mark
            elif not has_student_ans:
                status = 'unattempted'
                mark = 0.0
            elif str(student_ans).strip().upper() == c_upper:
                status = 'correct'
                mark = correct_mark
            else:
                status = 'wrong'
                mark = wrong_mark

        per_question[q_num] = {
            'student_answer': student_ans,
            'correct_answer': correct_ans,
            'status': status,
            'mark': mark,
        }

    return per_question


def validate_answer_key(answer_key, expected_count=100):
    """
    Answer key valid কিনা check করো।

    Args:
        answer_key (dict): Answer key to validate
        expected_count (int): Expected number of questions

    Returns:
        dict: {'valid': bool, 'errors': list}
    """
    errors = []
    valid_options = {'A', 'B', 'C', 'D', 'ANY', 'NONE'}

    if not isinstance(answer_key, dict):
        return {'valid': False, 'errors': ['Answer key must be a dictionary']}

    if len(answer_key) != expected_count:
        errors.append(f"Expected {expected_count} questions, got {len(answer_key)}")

    for q_num, answer in answer_key.items():
        if str(answer).upper() not in valid_options:
            errors.append(f"Q{q_num}: Invalid answer '{answer}' (must be A/B/C/D/ANY/NONE)")

    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'question_count': len(answer_key),
    }
