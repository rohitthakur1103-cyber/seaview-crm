import csv
import io
import logging
import re
import zipfile
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CELL_REF_RE = re.compile(r"([A-Za-z]+)")
_SPREADSHEET_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_RELATIONSHIP_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
_OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def validate_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip().lower()))


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(cleaned)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    timezone_offsets = {"EDT": "-0400", "EST": "-0500"}
    dated_with_offset = cleaned
    for zone, offset in timezone_offsets.items():
        if dated_with_offset.endswith(f" {zone}"):
            dated_with_offset = dated_with_offset[: -len(zone)] + offset
            break
    for pattern in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d-%b-%Y %I:%M %p %z",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            parsed = datetime.strptime(dated_with_offset, pattern)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def normalize_header(header: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in header).strip("_")


def split_name(full_name: str) -> tuple[str, str]:
    pieces = [piece for piece in full_name.split() if piece]
    if not pieces:
        return "", ""
    if len(pieces) == 1:
        return pieces[0], ""
    return pieces[0], " ".join(pieces[1:])


def to_float(value: str) -> float:
    cleaned = value.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def to_int(value: str) -> int:
    cleaned = value.strip()
    if not cleaned:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def to_bool(value: str) -> int:
    return 1 if value.strip().lower() in {"1", "true", "yes", "y", "on", "subscribed"} else 0


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def parsed_timestamp(value: str) -> str | None:
    parsed = parse_datetime(value.strip())
    return parsed.isoformat() if parsed else (value.strip() or None)


def parse_csv_bytes(payload: bytes) -> list[dict]:
    text = payload.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


def spreadsheet_column_index(cell_reference: str) -> int:
    match = _CELL_REF_RE.match(cell_reference or "")
    if not match:
        return 0
    index = 0
    for character in match.group(1).upper():
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


def first_worksheet_path(archive: zipfile.ZipFile) -> str:
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ElementTree.ParseError):
        return "xl/worksheets/sheet1.xml"

    first_sheet = workbook.find(".//main:sheet", _SPREADSHEET_NS)
    if first_sheet is None:
        return "xl/worksheets/sheet1.xml"

    relationship_id = first_sheet.attrib.get(f"{_OFFICE_REL_NS}id")
    if not relationship_id:
        return "xl/worksheets/sheet1.xml"

    for relationship in relationships.findall("rel:Relationship", _RELATIONSHIP_NS):
        if relationship.attrib.get("Id") != relationship_id:
            continue
        target = relationship.attrib.get("Target", "worksheets/sheet1.xml")
        if target.startswith("/"):
            return target.lstrip("/")
        return f"xl/{target}" if not target.startswith("xl/") else target

    return "xl/worksheets/sheet1.xml"


def parse_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("main:si", _SPREADSHEET_NS):
        strings.append("".join(text.text or "" for text in item.findall(".//main:t", _SPREADSHEET_NS)))
    return strings


def spreadsheet_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//main:t", _SPREADSHEET_NS)).strip()

    value_node = cell.find("main:v", _SPREADSHEET_NS)
    if value_node is None or value_node.text is None:
        return ""

    raw_value = value_node.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)].strip()
        except (ValueError, IndexError):
            return raw_value
    if cell_type == "b":
        return "TRUE" if raw_value == "1" else "FALSE"
    return raw_value


def parse_xlsx_bytes(payload: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        worksheet_path = first_worksheet_path(archive)
        shared_strings = parse_shared_strings(archive)
        try:
            root = ElementTree.fromstring(archive.read(worksheet_path))
        except (KeyError, ElementTree.ParseError) as exc:
            raise ValueError("Unable to read the first worksheet.") from exc

    raw_rows: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", _SPREADSHEET_NS):
        values_by_index: dict[int, str] = {}
        for cell in row.findall("main:c", _SPREADSHEET_NS):
            column_index = spreadsheet_column_index(cell.attrib.get("r", ""))
            values_by_index[column_index] = spreadsheet_cell_value(cell, shared_strings)
        if not values_by_index:
            continue
        max_index = max(values_by_index)
        values = [values_by_index.get(index, "") for index in range(max_index + 1)]
        if any(value.strip() for value in values):
            raw_rows.append(values)

    if not raw_rows:
        return []

    header_index = next((index for index, row in enumerate(raw_rows) if any(cell.strip() for cell in row)), 0)
    headers = [header.strip() or f"Column {index + 1}" for index, header in enumerate(raw_rows[header_index])]
    rows: list[dict] = []
    for raw_row in raw_rows[header_index + 1 :]:
        padded = raw_row + [""] * (len(headers) - len(raw_row))
        row = {header: padded[index].strip() if index < len(padded) else "" for index, header in enumerate(headers)}
        if any(value for value in row.values()):
            rows.append(row)
    return rows


def parse_table_bytes(filename: str, payload: bytes) -> list[dict]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xlsx":
        return parse_xlsx_bytes(payload)
    if suffix == ".csv":
        return parse_csv_bytes(payload)
    raise ValueError("Unsupported file type.")


def preview_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return list(rows[0].keys())


def preview_rows(rows: list[dict], limit: int = 5) -> list[dict]:
    return rows[:limit]


def merge_list_text(*values: str) -> str:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        for piece in value.split(","):
            cleaned = piece.strip()
            lowered = cleaned.lower()
            if cleaned and lowered not in seen:
                seen.add(lowered)
                items.append(cleaned)
    return ", ".join(items)


def merge_notes(*values: str | None) -> str | None:
    notes: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            notes.append(cleaned)
    return " | ".join(notes) if notes else None


def max_timestamp(*values: str | None) -> str | None:
    parsed = [(parse_datetime(v), v) for v in values if v]
    parsed = [item for item in parsed if item[0]]
    if not parsed:
        return next((v for v in values if v), None)
    return max(parsed, key=lambda item: item[0])[1]


def display_name(row: object) -> str:
    if hasattr(row, "__getitem__"):
        first = row["first_name"] or ""
        last = row["last_name"] or ""
    else:
        first = getattr(row, "first_name", "") or ""
        last = getattr(row, "last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or "Unnamed Customer"


def yes_no(value: int | None) -> str:
    return "Yes" if value else "No"


def display_timestamp(value: str | None, *, include_time: bool = True) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return value or ""
    if include_time:
        return parsed.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def build_name_location_match_value(first_name: str, last_name: str, city: str, state: str) -> str:
    display = (
        " ".join(part for part in [first_name.strip(), last_name.strip()] if part).strip()
        or "Unnamed customer"
    )
    location = ", ".join(part for part in [city.strip(), state.strip()] if part)
    return f"{display} · {location}" if location else display


def infer_preferred_channel(email: str, phone: str, preferred_channel: str = "") -> str | None:
    if preferred_channel:
        return preferred_channel
    if email and phone:
        return "either"
    if email:
        return "email"
    if phone:
        return "sms"
    return None


def display_upload_name(filename: str | None) -> str:
    name = Path(filename or "").name
    if len(name) > 15 and name[:14].isdigit() and name[14] == "_":
        return name[15:]
    return name
