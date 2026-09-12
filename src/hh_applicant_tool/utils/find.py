from __future__ import annotations


def find_key(data, target_key):
    """Рекурсивно ищет значение по ключу в вложенных dict/list структурах."""
    if isinstance(data, dict):
        if target_key in data:
            return data[target_key]
        for value in data.values():
            result = find_key(value, target_key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_key(item, target_key)
            if result is not None:
                return result
    return None
