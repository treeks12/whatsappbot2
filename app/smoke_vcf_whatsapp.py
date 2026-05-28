"""Smoke test do parser VCF para arquivos exportados pelo WhatsApp/iPhone.

Cobre os casos mais comuns que apareciam como "lixo":
- TEL com parametro waid= (canonico do WhatsApp)
- Linhas X-WA-* / X-ABLABEL / outras X-* que devem ser ignoradas
- Prefixo itemN.TEL (Apple/iOS)
- TEL com numero local sem DDI no value, mas com DDI dentro do waid
- Multiplos contatos no mesmo .vcf
"""

from app.csv_utils import parse_contacts_vcf_text


def test_whatsapp_vcf_with_waid():
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "N:;Joao Silva;;;\r\n"
        "FN:Joao Silva\r\n"
        "TEL;type=CELL;type=VOICE;waid=5511988887777:+55 11 98888-7777\r\n"
        "X-WA-BIZ-NAME:Loja do Joao\r\n"
        "X-WA-BIZ-DESCRIPTION:vendas em geral\r\n"
        "END:VCARD\r\n"
    )
    contacts = parse_contacts_vcf_text(vcf)
    assert len(contacts) == 1, contacts
    assert contacts[0]["phone"] == "5511988887777", contacts[0]
    assert contacts[0]["name"] == "Joao Silva", contacts[0]
    print(f"whatsapp_with_waid.ok phone={contacts[0]['phone']}")


def test_value_without_ddi_uses_waid():
    """O 'value' veio como '11988887777' sem DDI; waid traz '5511988887777'.

    Sem o fix, o parser cairia no value e perderia o DDI.
    """
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Maria\r\n"
        "TEL;type=CELL;waid=5511988887777:11988887777\r\n"
        "END:VCARD\r\n"
    )
    contacts = parse_contacts_vcf_text(vcf)
    assert len(contacts) == 1, contacts
    assert contacts[0]["phone"] == "5511988887777", contacts[0]
    print(f"waid_overrides_local_value.ok phone={contacts[0]['phone']}")


def test_apple_item_prefix():
    """iPhone/iOS as vezes exporta com prefixo item1.TEL."""
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Carlos\r\n"
        "item1.TEL;type=CELL;waid=5521977776666:+55 21 97777-6666\r\n"
        "item1.X-ABLabel:WhatsApp\r\n"
        "END:VCARD\r\n"
    )
    contacts = parse_contacts_vcf_text(vcf)
    assert len(contacts) == 1, contacts
    assert contacts[0]["phone"] == "5521977776666", contacts[0]
    assert contacts[0]["name"] == "Carlos", contacts[0]
    print(f"apple_item_prefix.ok phone={contacts[0]['phone']}")


def test_x_extensions_ignored():
    """Linhas X-* nao podem ser tratadas como TEL nem quebrar o parser."""
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Ana\r\n"
        "X-WA-BIZ-NAME:Empresa\r\n"
        "X-WA-BIZ-DESCRIPTION:descricao com numeros 123456 que nao sao telefone\r\n"
        "X-ABLABEL:trabalho\r\n"
        "TEL;type=CELL:+55 31 98765-4321\r\n"
        "END:VCARD\r\n"
    )
    contacts = parse_contacts_vcf_text(vcf)
    assert len(contacts) == 1, contacts
    assert contacts[0]["phone"] == "5531987654321", contacts[0]
    print(f"x_extensions_ignored.ok phone={contacts[0]['phone']}")


def test_multiple_cards():
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Contato 1\r\n"
        "TEL;waid=5511988887777:+55 11 98888-7777\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Contato 2\r\n"
        "TEL;waid=5521977776666:+55 21 97777-6666\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Repetido\r\n"
        "TEL;waid=5511988887777:+55 11 98888-7777\r\n"
        "END:VCARD\r\n"
    )
    contacts = parse_contacts_vcf_text(vcf)
    # Espera 2 unicos (terceiro e duplicata).
    phones = [c["phone"] for c in contacts]
    assert len(contacts) == 2, contacts
    assert "5511988887777" in phones and "5521977776666" in phones, phones
    print(f"multiple_cards.ok unique={len(contacts)}")


def test_quoted_printable_name_still_works():
    """Regressao: nao quebrar o decode de QUOTED-PRINTABLE existente."""
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:2.1\r\n"
        "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Jo=C3=A3o;;;;\r\n"
        "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Jo=C3=A3o\r\n"
        "TEL;CELL:+55 11 99999-8888\r\n"
        "END:VCARD\r\n"
    )
    contacts = parse_contacts_vcf_text(vcf)
    assert len(contacts) == 1, contacts
    name = contacts[0]["name"]
    assert "Jo" in name, name.encode("ascii", "backslashreplace")
    assert contacts[0]["phone"] == "5511999998888", contacts[0]
    print("quoted_printable.ok len_name=" + str(len(name)))


def main():
    test_whatsapp_vcf_with_waid()
    test_value_without_ddi_uses_waid()
    test_apple_item_prefix()
    test_x_extensions_ignored()
    test_multiple_cards()
    test_quoted_printable_name_still_works()
    print("smoke_vcf_whatsapp.ok")


if __name__ == "__main__":
    main()
