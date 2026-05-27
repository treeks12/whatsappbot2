from .telegram_bot import qr_photo_bytes


def main():
    assert qr_photo_bytes("") is None
    assert qr_photo_bytes("not-base64") is None
    assert qr_photo_bytes("YWJj") == b"abc"
    print("qr.empty_payload_guard.ok")


if __name__ == "__main__":
    main()
