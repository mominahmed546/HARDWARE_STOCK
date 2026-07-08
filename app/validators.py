import re
from decimal import Decimal, InvalidOperation


class ValidationErrors:
    def __init__(self):
        self.errors = {}

    def add(self, field, message):
        self.errors[field] = message

    @property
    def valid(self):
        return not self.errors

    def first(self):
        if self.errors:
            return next(iter(self.errors.values()))
        return None


def _label(field, label):
    return label or field.replace("_", " ").title()


def clean_string(value, field, errors, required=True, min_len=1, max_len=100, label=None):
    name = _label(field, label)
    value = (value or "").strip()

    if not value:
        if required:
            errors.add(field, f"{name} is required.")
        return None

    if len(value) < min_len:
        errors.add(field, f"{name} must be at least {min_len} characters.")
        return None

    if len(value) > max_len:
        errors.add(field, f"{name} must be at most {max_len} characters.")
        return None

    return value


def clean_username(value, errors):
    username = clean_string(value, "username", errors, min_len=3, max_len=50, label="Username")
    if username and not re.match(r"^[A-Za-z0-9_]+$", username):
        errors.add("username", "Username may only contain letters, numbers, and underscores.")
        return None
    return username


def clean_password(value, errors, field="password", min_len=6):
    name = _label(field, "Password")
    value = value or ""

    if not value.strip():
        errors.add(field, f"{name} is required.")
        return None

    if len(value) < min_len:
        errors.add(field, f"{name} must be at least {min_len} characters.")
        return None

    return value


def clean_optional_string(value, field, errors, max_len=100, label=None):
    return clean_string(value, field, errors, required=False, min_len=0, max_len=max_len, label=label)


def clean_phone(value, field, errors, required=False, max_len=20):
    name = _label(field, label="Contact number")
    value = (value or "").strip()

    if not value:
        if required:
            errors.add(field, f"{name} is required.")
        return None

    if len(value) > max_len:
        errors.add(field, f"{name} must be at most {max_len} characters.")
        return None

    if not re.match(r"^[0-9+\-\s()]{7,20}$", value):
        errors.add(field, f"{name} must be a valid phone number.")
        return None

    return value


def clean_positive_int(value, field, errors, required=True, min_val=0, label=None):
    name = _label(field, label)
    value = (value or "").strip()

    if not value:
        if required:
            errors.add(field, f"{name} is required.")
        return None

    try:
        number = int(value)
    except ValueError:
        errors.add(field, f"{name} must be a whole number.")
        return None

    if number < min_val:
        errors.add(field, f"{name} must be at least {min_val}.")
        return None

    return number


def clean_positive_decimal(value, field, errors, required=True, min_val=0, label=None):
    name = _label(field, label)
    value = (value or "").strip()

    if not value:
        if required:
            errors.add(field, f"{name} is required.")
        return None

    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        errors.add(field, f"{name} must be a valid number.")
        return None

    if number < min_val:
        errors.add(field, f"{name} must be at least {min_val}.")
        return None

    return float(number)


def clean_select_id(value, field, errors, label=None):
    return clean_positive_int(value, field, errors, required=True, min_val=1, label=label)


def clean_optional_select_id(value, field, errors, label=None):
    value = (value or "").strip()
    if not value:
        return None
    return clean_positive_int(value, field, errors, required=True, min_val=1, label=label)


def clean_date(value, field, errors, label=None):
    name = _label(field, label or "Date")
    value = (value or "").strip()

    if not value:
        errors.add(field, f"{name} is required.")
        return None

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        errors.add(field, f"{name} must be a valid date.")
        return None

    return value
