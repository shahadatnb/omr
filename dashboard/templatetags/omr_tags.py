from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """
    Safely get value from dictionary by key in Django templates.
    """
    if not dictionary or not isinstance(dictionary, dict):
        return '-'
    val = dictionary.get(key)
    if val is not None:
        if isinstance(val, (int, float)):
            return f"{val:.2f}"
        return val
    return '-'
