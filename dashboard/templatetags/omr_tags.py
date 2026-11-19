from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """
    Safely get value from dictionary by key in Django templates.
    Supports both scalar marks and nested dicts with 'marks'.
    """
    if not dictionary or not isinstance(dictionary, dict):
        return '-'
    val = dictionary.get(key)
    if val is not None:
        if isinstance(val, dict):
            m = val.get('marks', 0)
            try:
                return f"{float(m):.2f}"
            except (ValueError, TypeError):
                return str(m)
        if isinstance(val, (int, float)):
            return f"{val:.2f}"
        return val
    return '-'

@register.filter
def get_subject_stat(dictionary, key):
    """
    Safely extract structured subject stats:
    {
        'marks': '12.50',
        'correct': 13,
        'wrong': 2,
        'unattempted': 0,
        'has_details': True
    }
    Supports both new dict structure and legacy scalar number.
    """
    if not dictionary or not isinstance(dictionary, dict):
        return None
    val = dictionary.get(key)
    if val is None:
        return None
    if isinstance(val, dict):
        m = val.get('marks', 0)
        try:
            m_str = f"{float(m):.2f}"
        except (ValueError, TypeError):
            m_str = str(m)
        has_details = ('correct' in val) or ('wrong' in val)
        return {
            'marks': m_str,
            'correct': val.get('correct', 0),
            'wrong': val.get('wrong', 0),
            'unattempted': val.get('unattempted', 0),
            'has_details': has_details,
        }
    try:
        m_str = f"{float(val):.2f}"
    except (ValueError, TypeError):
        m_str = str(val)
    return {
        'marks': m_str,
        'correct': None,
        'wrong': None,
        'unattempted': None,
        'has_details': False,
    }
