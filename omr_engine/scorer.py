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


def calculate_score(student_answers, answer_key, correct_mark=1.0, wrong_mark=-0.25):
    """
    নেগেটিভ মার্কিং সহ MCQ স্কোর হিসাব করো।

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

    Returns:
        dict: স্কোর summary:
              - 'correct': সঠিক উত্তরের সংখ্যা
              - 'wrong': ভুল উত্তরের সংখ্যা
              - 'unattempted': উত্তর না দেওয়া প্রশ্নের সংখ্যা
              - 'marks': মোট প্রাপ্ত marks (float)
              - 'total_questions': মোট প্রশ্ন সংখ্যা
              - 'percentage': শতকরা নম্বর
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

        # উত্তরের মান নির্ধারণ করো
        if student_ans is None:
            unattempted += 1
            logger.debug(f"Q{q_num}: Unattempted")
        elif str(student_ans).upper() == str(correct_ans).upper():
            correct += 1
            logger.debug(f"Q{q_num}: Correct ({student_ans})")
        else:
            wrong += 1
            logger.debug(f"Q{q_num}: Wrong (student={student_ans}, correct={correct_ans})")

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
        elif student_ans is None:
            status = 'unattempted'
            mark = 0.0
        elif str(student_ans).upper() == str(correct_ans).upper():
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
    valid_options = {'A', 'B', 'C', 'D'}

    if not isinstance(answer_key, dict):
        return {'valid': False, 'errors': ['Answer key must be a dictionary']}

    if len(answer_key) != expected_count:
        errors.append(f"Expected {expected_count} questions, got {len(answer_key)}")

    for q_num, answer in answer_key.items():
        if str(answer).upper() not in valid_options:
            errors.append(f"Q{q_num}: Invalid answer '{answer}' (must be A/B/C/D)")

    return {
        'valid': len(errors) == 0,
        'errors': errors,
        'question_count': len(answer_key),
    }
