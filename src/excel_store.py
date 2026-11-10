import ipaddress
import math
import os
import zipfile
from datetime import date, datetime, time, timedelta

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

import config


def _parse_hour(value):
    if isinstance(value, datetime):
        return value.hour
    if isinstance(value, time):
        return value.hour
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 0 <= value < 1:
            return round(value * 24) % 24
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(text, fmt).hour
        except ValueError:
            continue
    return None


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in (
            "%Y-%m-%d",
            "%d-%b-%y",
            "%d-%b-%Y",
            "%m/%d/%Y",
            "%d/%m/%Y",
        ):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def _find_date_and_hour_cells(sheet, target_date, target_hour):
    for row in sheet.iter_rows():
        date_columns = [
            cell.column
            for cell in row
            if isinstance(cell.value, str) and cell.value.strip().casefold() == "date"
        ]
        if not date_columns:
            continue

        hour_columns = {
            cell.column
            for cell in row
            if _parse_hour(cell.value) == target_hour
        }
        if not hour_columns:
            continue

        for date_column in date_columns:
            hour_column = next(
                (column for column in sorted(hour_columns) if column > date_column),
                None,
            )
            if hour_column is None:
                continue
            for row_number in range(row[0].row + 1, sheet.max_row + 1):
                if _parse_date(sheet.cell(row_number, date_column).value) == target_date:
                    return sheet.cell(row_number, hour_column)
    return None


def _parse_record(record):
    if record.get("monthly_backup") is True or str(
        record.get("status") or ""
    ).strip().casefold() == "monthly":
        return None, None
    if any(
        isinstance(record.get(metric), str)
        and record[metric].strip().casefold() == "monthly"
        for metric in ("asp", "cpu")
    ):
        return None, None

    timestamp = record.get("timestamp")
    try:
        recorded_at = datetime.strptime(str(timestamp), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None, f"Invalid record timestamp for Excel export: {timestamp!r}"

    sheet_name = ""
    for candidate in (
        record.get("host_name"),
        record.get("server_name"),
        record.get("server"),
        record.get("lpar"),
        record.get("config_key"),
    ):
        candidate_name = str(candidate or "").strip()
        if not candidate_name:
            continue
        try:
            ipaddress.ip_address(candidate_name)
        except ValueError:
            sheet_name = candidate_name
            break
    if not sheet_name:
        sheet_name = _configured_sheet_name(record)
    if not sheet_name:
        return None, "Record has no non-IP server name for Excel worksheet selection"

    values = {}
    for metric in ("asp", "cpu"):
        raw_value = record.get(metric)
        try:
            percentage = float(raw_value)
            if not math.isfinite(percentage) or percentage < 0 or (
                metric == "asp" and percentage > 100
            ):
                raise ValueError(f"Invalid {metric.upper()} percentage: {raw_value!r}")
            values[metric] = percentage
        except (TypeError, ValueError) as exc:
            return None, str(exc)

    return {
        "sheet_name": sheet_name,
        "recorded_at": recorded_at,
        "values": values,
    }, None


def _configured_sheet_name(record):
    record_ips = set()
    for field in ("host_name", "server_name", "server", "lpar", "config_key", "ip"):
        candidate = str(record.get(field) or "").strip()
        try:
            record_ips.add(ipaddress.ip_address(candidate))
        except ValueError:
            continue

    if not record_ips:
        return ""

    for server_name, server_config in config.SERVER_CONFIGS.items():
        host = (
            server_config.get("host", "")
            if isinstance(server_config, dict)
            else server_config
        )
        try:
            if ipaddress.ip_address(str(host).strip()) in record_ips:
                return str(server_name).strip()
        except ValueError:
            continue
    return ""


def _update_workbook_batch(path, metric, records):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Workbook not found: {path}")

    lock_path = os.path.join(os.path.dirname(path), f"~${os.path.basename(path)}")
    if os.path.exists(lock_path):
        raise PermissionError(f"Workbook is open in Excel: {path}")

    workbook = load_workbook(path)
    updated_count = 0
    skipped_records = []
    try:
        for record in records:
            sheet_name = record["sheet_name"]
            sheet = next(
                (
                    item
                    for item in workbook.worksheets
                    if item.title.casefold() == sheet_name.casefold()
                ),
                None,
            )
            if sheet is None:
                raise KeyError(
                    f"Worksheet {sheet_name!r} was not found in {path}"
                )

            recorded_at = record["recorded_at"]
            cell = _find_date_and_hour_cells(
                sheet,
                recorded_at.date(),
                recorded_at.hour,
            )
            if cell is None:
                raise ValueError(
                    f"No cell found for {recorded_at.date().isoformat()} at "
                    f"{recorded_at.hour:02d}:00 in worksheet {sheet_name!r}"
                )

            if cell.value is not None and str(cell.value).strip():
                skipped_records.append(
                    f"{sheet_name} {recorded_at.date().isoformat()} "
                    f"{recorded_at.hour:02d}:00"
                )
                continue

            cell.value = record["values"][metric] / 100
            if cell.number_format == "General":
                cell.number_format = "0.00%"
            updated_count += 1

        if updated_count:
            workbook.save(path)
    finally:
        workbook.close()
    return updated_count, skipped_records


def _update_monthly_marker_batch(path, metric, records):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Workbook not found: {path}")

    lock_path = os.path.join(os.path.dirname(path), f"~${os.path.basename(path)}")
    if os.path.exists(lock_path):
        raise PermissionError(f"Workbook is open in Excel: {path}")

    workbook = load_workbook(path)
    updated_count = 0
    try:
        for record in records:
            sheet = next(
                (
                    item
                    for item in workbook.worksheets
                    if item.title.casefold() == record["sheet_name"].casefold()
                ),
                None,
            )
            if sheet is None:
                raise KeyError(
                    f"Worksheet {record['sheet_name']!r} was not found in {path}"
                )

            recorded_at = record["recorded_at"]
            cell = _find_date_and_hour_cells(
                sheet,
                recorded_at.date(),
                recorded_at.hour,
            )
            if cell is None:
                raise ValueError(
                    f"No cell found for {recorded_at.date().isoformat()} at "
                    f"{recorded_at.hour:02d}:00 in worksheet {record['sheet_name']!r}"
                )

            if cell.value is None or not str(cell.value).strip():
                cell.value = "MONTHLY"
                updated_count += 1
            elif (
                isinstance(cell.value, (int, float))
                and not isinstance(cell.value, bool)
                and cell.value == 0
            ):
                cell.value = "MONTHLY"
                updated_count += 1

        if updated_count:
            workbook.save(path)
    finally:
        workbook.close()
    return updated_count


def push_monthly_backup_to_excel(backup_data, target_date, server_names=None):
    """Mark hourly workbook cells covered by a monthly backup interval."""
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    if not isinstance(target_date, date):
        raise TypeError("target_date must be a date or datetime")

    server_names = {
        str(key).strip().casefold(): str(value).strip()
        for key, value in (server_names or {}).items()
        if str(key).strip() and str(value).strip()
    }
    day_start = datetime.combine(target_date, time.min)
    day_end = day_start + timedelta(days=1)
    marked_slots = {}

    for server_key, records in backup_data.items():
        if server_key == "_errors":
            continue
        server_identity = str(server_key).strip().casefold()
        sheet_name = server_names.get(server_identity, "")
        if not sheet_name:
            try:
                ipaddress.ip_address(str(server_key).strip())
            except ValueError:
                sheet_name = str(server_key).strip()
            else:
                sheet_name = _configured_sheet_name({"config_key": server_key})
        if not sheet_name:
            return [
                f"Excel monthly batch update failed: no worksheet mapping for "
                f"backup server {server_key!r}"
            ]

        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            continue

        for record in records:
            if not isinstance(record, dict) or not record.get("start_time"):
                continue
            try:
                start = datetime.fromisoformat(
                    str(record["start_time"]).replace("Z", "+00:00")
                )
                end_value = record.get("end_time")
                end = (
                    datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
                    if end_value
                    else datetime.now(start.tzinfo)
                )
            except (TypeError, ValueError):
                return [
                    f"Excel monthly batch update failed: invalid backup interval "
                    f"for server {server_key!r}"
                ]

            if start.tzinfo is not None:
                start = start.astimezone().replace(tzinfo=None)
            if end.tzinfo is not None:
                end = end.astimezone().replace(tzinfo=None)
            interval_start = max(start, day_start)
            interval_end = min(end, day_end)
            if interval_end <= interval_start:
                continue

            hour_start = interval_start.replace(minute=0, second=0, microsecond=0)
            while hour_start < interval_end:
                slot = hour_start.strftime("%Y-%m-%d %H")
                marked_slots[(sheet_name, slot)] = {
                    "sheet_name": sheet_name,
                    "recorded_at": hour_start,
                }
                hour_start += timedelta(hours=1)

    if not marked_slots:
        return []

    records = list(marked_slots.values())
    year = target_date.strftime("%Y")
    messages = []
    for metric in ("asp", "cpu"):
        workbook_path = config.get_excel_workbook_path(metric, year)
        try:
            updated_count = _update_monthly_marker_batch(
                workbook_path,
                metric,
                records,
            )
            if updated_count:
                messages.append(
                    f"{metric.upper()} Excel monthly batch updated: "
                    f"{updated_count} cell(s)"
                )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
            InvalidFileException,
        ) as exc:
            messages.append(
                f"{metric.upper()} Excel monthly batch update failed: {exc}"
            )

    return messages


def push_metrics_to_excel(records):
    """Update all server cells for an hour, saving each shared workbook once."""
    if isinstance(records, dict):
        records = [records]

    parsed_records = []
    errors = []
    for record in records:
        parsed, error = _parse_record(record)
        if error:
            errors.append(f"Excel export failed: {error}")
        elif parsed is not None:
            parsed_records.append(parsed)

    if errors:
        return errors
    if not parsed_records:
        return errors

    years = {record["recorded_at"].strftime("%Y") for record in parsed_records}
    hours = {record["recorded_at"].strftime("%Y-%m-%d %H") for record in parsed_records}
    if len(years) != 1 or len(hours) != 1:
        return errors + ["Excel batch must contain readings from one hour"]
    year = years.pop()

    for metric in ("asp", "cpu"):
        workbook_path = config.get_excel_workbook_path(metric, year)
        try:
            updated_count, skipped_records = _update_workbook_batch(
                workbook_path,
                metric,
                parsed_records,
            )
            if updated_count and skipped_records:
                errors.append(
                    f"{metric.upper()} Excel batch updated: {updated_count} server(s)"
                )
            if skipped_records:
                errors.extend(
                    f"{metric.upper()} Excel batch skipped: already has log at {slot}"
                    for slot in skipped_records
                )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
            InvalidFileException,
        ) as exc:
            errors.append(f"{metric.upper()} Excel batch update failed: {exc}")

    return errors


def push_daily_metrics_to_excel(records):
    """Replay a day's server readings while opening each shared workbook only once."""
    if isinstance(records, dict):
        records = [records]

    parsed_records = []
    errors = []
    for record in records:
        parsed, error = _parse_record(record)
        if error:
            errors.append(f"Excel export failed: {error}")
        elif parsed is not None:
            parsed_records.append(parsed)

    if not parsed_records:
        return errors

    years = {record["recorded_at"].strftime("%Y") for record in parsed_records}
    dates = {record["recorded_at"].date() for record in parsed_records}
    if len(years) != 1 or len(dates) != 1:
        return errors + ["Excel catch-up batch must contain readings from one day"]
    year = years.pop()

    for metric in ("asp", "cpu"):
        workbook_path = config.get_excel_workbook_path(metric, year)
        try:
            updated_count, skipped_records = _update_workbook_batch(
                workbook_path,
                metric,
                parsed_records,
            )
            if updated_count:
                errors.append(
                    f"{metric.upper()} Excel daily batch updated: "
                    f"{updated_count} server-hour(s)"
                )
            if skipped_records:
                errors.append(
                    f"{metric.upper()} Excel daily batch skipped: "
                    f"{len(skipped_records)} existing cell(s)"
                )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
            InvalidFileException,
        ) as exc:
            errors.append(f"{metric.upper()} Excel daily batch update failed: {exc}")

    return errors
