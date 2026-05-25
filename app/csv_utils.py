import csv
import quopri
import zipfile
from pathlib import Path

from .evolution import normalize_phone


PHONE_COLUMNS = ("telefone", "phone", "numero", "número", "celular", "whatsapp")
NAME_COLUMNS = ("nome", "name", "cliente")


def parse_contacts_file(path: Path, max_contacts: int) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_contacts_csv(path, max_contacts)
    if suffix == ".vcf":
        return parse_contacts_vcf(path, max_contacts)
    if suffix == ".zip":
        return parse_contacts_zip(path, max_contacts)
    raise ValueError("Envie um arquivo .csv, .vcf ou .zip.")


def parse_contacts_csv(path: Path, max_contacts: int) -> list[dict]:
    content = path.read_text(encoding="utf-8-sig")
    contacts = parse_contacts_csv_text(content)
    validate_contacts(contacts, max_contacts, "Arquivo")
    return contacts


def parse_contacts_csv_text(content: str) -> list[dict]:
    reader = csv.DictReader(content.splitlines())
    contacts = []
    seen = set()

    for index, row in enumerate(reader):
        normalized = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
        phone = usable_phone(first_value(normalized, PHONE_COLUMNS))
        if not phone or phone in seen:
            continue
        seen.add(phone)

        contacts.append(
            {
                "row_index": index,
                "name": first_value(normalized, NAME_COLUMNS) or "Cliente",
                "phone": phone,
            }
        )

    return contacts


def parse_contacts_vcf(path: Path, max_contacts: int) -> list[dict]:
    content = path.read_text(encoding="utf-8", errors="replace")
    contacts = parse_contacts_vcf_text(content)
    validate_contacts(contacts, max_contacts, "VCF")
    return contacts


def parse_contacts_zip(path: Path, max_contacts: int) -> list[dict]:
    contacts = []
    seen = set()

    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue

            suffix = Path(member.filename).suffix.lower()
            if suffix not in (".csv", ".vcf"):
                continue

            content = archive.read(member).decode("utf-8-sig", errors="replace")
            parsed = parse_contacts_csv_text(content) if suffix == ".csv" else parse_contacts_vcf_text(content)

            for item in parsed:
                phone = item["phone"]
                if phone in seen:
                    continue
                seen.add(phone)
                item = dict(item)
                item["row_index"] = len(contacts)
                contacts.append(item)

    validate_contacts(contacts, max_contacts, "ZIP")
    return contacts


def parse_contacts_vcf_text(content: str) -> list[dict]:
    cards = split_vcards(unfold_vcard_lines(content.splitlines()))
    contacts = []
    seen = set()

    for card_index, card in enumerate(cards):
        name = "Cliente"
        phones = []

        for line in card:
            key, params, value = parse_vcard_line(line)
            if key in ("FN", "N"):
                decoded = decode_vcard_value(value, params)
                if decoded:
                    name = clean_vcard_name(decoded) or name
            elif key == "TEL":
                phone = usable_phone(value)
                if phone:
                    phones.append(phone)

        for phone in unique_values(phones):
            if phone in seen:
                continue
            seen.add(phone)
            contacts.append({"row_index": card_index, "name": name, "phone": phone})

    return contacts


def validate_contacts(contacts: list[dict], max_contacts: int, label: str):
    if not contacts:
        raise ValueError(f"{label} sem contatos validos.")

    if len(contacts) > max_contacts:
        raise ValueError(f"{label} tem {len(contacts)} contatos; limite atual e {max_contacts}.")


def unfold_vcard_lines(lines: list[str]) -> list[str]:
    unfolded = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line.rstrip("\r\n"))
    return unfolded


def split_vcards(lines: list[str]) -> list[list[str]]:
    cards = []
    current = None
    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VCARD":
            current = []
        elif upper == "END:VCARD":
            if current:
                cards.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return cards


def parse_vcard_line(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        return "", {}, ""

    left, value = line.split(":", 1)
    parts = left.split(";")
    key = parts[0].upper()
    params = {}

    for part in parts[1:]:
        if "=" in part:
            param_key, param_value = part.split("=", 1)
            params[param_key.upper()] = param_value.upper()
        else:
            params[part.upper()] = "TRUE"

    return key, params, value.strip()


def decode_vcard_value(value: str, params: dict[str, str]) -> str:
    if params.get("ENCODING") == "QUOTED-PRINTABLE":
        charset = params.get("CHARSET", "UTF-8")
        decoded = quopri.decodestring(value)
        try:
            return decoded.decode(charset, errors="replace").strip()
        except LookupError:
            return decoded.decode("utf-8", errors="replace").strip()
    return value.strip()


def clean_vcard_name(value: str) -> str:
    parts = [part.strip() for part in value.split(";") if part.strip()]
    return " ".join(parts).strip() if ";" in value else value.strip()


def usable_phone(value: str) -> str:
    digits = normalize_phone(value)
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits
    if not digits.startswith("55") and len(digits) >= 11:
        return digits
    return ""


def unique_values(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def first_value(row: dict, columns: tuple[str, ...]) -> str:
    for column in columns:
        value = row.get(column)
        if value:
            return value
    return ""


def mime_from_name(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"
