"""Phone number helpers and optional SMS.ru delivery."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request


def national_part(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 11 and digits[0] in "78":
        return digits[1:11]
    if digits.startswith("7"):
        return digits[1:11]
    return digits[:10]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif digits.startswith("7") and len(digits) == 11:
        pass
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    elif 0 < len(digits) <= 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    raise ValueError("Введите 10 цифр номера после +7")


def format_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) != 11:
        return phone
    return f"+{digits[0]} {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def send_sms(api_id: str, phone: str, message: str) -> tuple[bool, str]:
    if not api_id.strip():
        return False, "SMS не настроено"
    query = urllib.parse.urlencode(
        {
            "api_id": api_id.strip(),
            "to": phone.lstrip("+"),
            "msg": message,
            "json": 1,
        }
    )
    try:
        with urllib.request.urlopen(f"https://sms.ru/sms/send?{query}", timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return False, str(exc)
    status = str(payload.get("status", "")).upper()
    if status == "OK":
        return True, "ok"
    return False, str(payload.get("status_text") or payload)
