"""
test_vapid_keys.py — регрессия на нормализацию VAPID-ключа для Web Push.

Корневая причина инцидента: pywebpush передаёт строку ключа в
py_vapid `Vapid.from_string()`, которая НЕ срезает PEM-обёртку
(-----BEGIN/END-----), а просто убирает переводы строк и
url-safe-base64-декодирует ВСЮ строку. Поэтому PEM ломается
("Could not deserialize key data ... ASN.1 parsing error: invalid length").
Единственный рабочий формат — single-line url-safe base64 PKCS8 DER.

Тест гарантирует, что `_normalize_vapid_key()` для любого поддерживаемого
входа отдаёт строку, которую принимает `Vapid.from_string()`, и что
результат — НЕ PEM.
"""
import base64
import importlib.util
import os
import sys

PASS = "✅"
FAIL = "❌"
results = []


def check(label, condition, detail=""):
    results.append((PASS if condition else FAIL, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


def _load_push_utils():
    spec = importlib.util.spec_from_file_location(
        "push_utils_under_test",
        os.path.join(os.path.dirname(__file__), "web", "push_utils.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    pu = _load_push_utils()
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption,
    )
    from py_vapid import Vapid02

    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    der = priv.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    url_single = base64.urlsafe_b64encode(der).decode().rstrip("=")
    std_single = base64.b64encode(der).decode()
    raw32 = base64.urlsafe_b64encode(
        priv.private_numbers().private_value.to_bytes(32, "big")
    ).decode().rstrip("=")

    cases = {
        "multiline PEM": pem,
        "collapsed PEM (Amvera strips newlines)": pem.replace("\n", ""),
        "escaped-newline PEM": pem.replace("\n", "\\n"),
        "quoted PEM": '"' + pem + '"',
        "url-safe DER single-line": url_single,
        "standard DER single-line": std_single,
        "raw 32-byte EC scalar": raw32,
    }

    for name, raw in cases.items():
        out = pu._normalize_vapid_key(raw)
        # 1) результат принимается pywebpush-путём
        ok_from_string = False
        try:
            Vapid02.from_string(private_key=out)
            ok_from_string = True
        except Exception as e:  # noqa: BLE001
            check(f"from_string OK: {name}", False, str(e)[:80])
        check(f"from_string OK: {name}", ok_from_string)
        # 2) результат — НЕ PEM (иначе регресс к старому багу)
        check(f"output is not PEM: {name}", "BEGIN" not in out, out[:30])
        # 3) _is_configured() распознал бы такой ключ как настроенный
        #    (повторяем логику _is_configured для compact-ветки)
        _compact = out.replace("\n", "").replace("=", "")
        is_cfg = ("BEGIN" in out and "KEY" in out) or (
            len(_compact) >= 20 and " " not in _compact
        )
        check(f"recognized as configured: {name}", is_cfg)

    # пустой/мусорный вход не падает
    try:
        check("empty input safe", pu._normalize_vapid_key("") == "")
        pu._normalize_vapid_key("not-a-key")
        check("garbage input safe", True)
    except Exception as e:  # noqa: BLE001
        check("garbage input safe", False, str(e)[:80])

    passed = sum(1 for r in results if r[0] == PASS)
    failed = sum(1 for r in results if r[0] == FAIL)
    print(f"\n  {'='*40}")
    print(f"  VAPID-ключ: всего {len(results)} | {PASS} {passed} | {FAIL} {failed}")
    print(f"  {'='*40}")
    if failed == 0:
        print(f"\n  🎉 Все {len(results)} проверок прошли!\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
