"""Smoke test de parsing basico do webhook da Evolution."""

from app.webhook import STATUS_DELIVERED, STATUS_READ, _normalize_status, _parse_jid


def main():
    assert 3 in STATUS_DELIVERED
    assert 4 in STATUS_READ
    assert 5 in STATUS_READ
    assert 4 not in STATUS_DELIVERED
    assert _normalize_status("4") in STATUS_READ
    assert _normalize_status("read") in STATUS_READ
    assert _normalize_status("delivery_ack") in STATUS_DELIVERED
    print("status_mapping.ok")

    assert _parse_jid("5511999998888@s.whatsapp.net") == ("5511999998888", None)
    assert _parse_jid("5511999998888@c.us") == ("5511999998888", None)
    assert _parse_jid("123456789@lid") == (None, "123456789@lid")
    assert _parse_jid("120363000000000@g.us") == (None, None)
    print("jid_parse.ok")
    print("smoke_webhook.ok")


if __name__ == "__main__":
    main()
