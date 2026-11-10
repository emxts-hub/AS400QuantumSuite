import json
import ipaddress
import math
import os
import re
import smtplib
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from config import get_app_data_dir, get_monthly_logs_dir_for, safe_json_save
from system_logging import SYSTEM_LOGGER
from ui.setcreds import load_weekly_report_email


_HOURS = tuple(range(24))
_METRICS = ("asp", "cpu")
_HEADER_FILL = PatternFill("solid", fgColor="1F6FEB")
_HEADER_FONT = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
_TITLE_FONT = Font(name="Segoe UI", size=16, bold=True, color="1F2937")
_SECTION_FONT = Font(name="Segoe UI", size=12, bold=True, color="1F6FEB")
_DATA_FONT = Font(name="Segoe UI", size=10, color="1F2937")
_BOLD_FONT = Font(name="Segoe UI", size=10, bold=True, color="1F2937")
_THIN_BORDER = Border(
    left=Side(style="thin", color="D0D7DE"),
    right=Side(style="thin", color="D0D7DE"),
    top=Side(style="thin", color="D0D7DE"),
    bottom=Side(style="thin", color="D0D7DE"),
)


def latest_complete_week(now=None):
    """Return the most recently completed Saturday-through-Friday period."""
    now = now or datetime.now()
    current_date = now.date() if isinstance(now, datetime) else now
    days_since_friday = (current_date.weekday() - 4) % 7
    if days_since_friday == 0:
        days_since_friday = 7
    week_end = current_date - timedelta(days=days_since_friday)
    return week_end - timedelta(days=6), week_end


def milliseconds_until_next_saturday_midnight(now=None):
    """Return the delay until the next local Saturday at midnight."""
    now = now or datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_run = (now + timedelta(days=days_until_saturday)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if next_run <= now:
        next_run += timedelta(days=7)
    return max(1, math.ceil((next_run - now).total_seconds() * 1000))


def build_weekly_report_path(week_start, week_end):
    week_start, week_end = _validate_week(week_start, week_end)
    month_dir = get_monthly_logs_dir_for(week_end.strftime("%Y-%m"))
    filename = (
        "IBM_i_Weekly_ASP_CPU_Matrix_"
        f"{week_start:%Y-%m-%d}_to_{week_end:%Y-%m-%d}.xlsx"
    )
    return os.path.join(month_dir, filename)


def generate_weekly_asp_cpu_report(week_start, week_end):
    """Create the weekly hourly matrix workbook from the persisted LPAR logs."""
    week_start, week_end = _validate_week(week_start, week_end)
    hourly_data = _read_weekly_hourly_data(week_start, week_end)
    if not hourly_data:
        return None

    report_path = build_weekly_report_path(week_start, week_end)
    workbook = Workbook()
    workbook.remove(workbook.active)

    sheet_names = {}
    for server in sorted(hourly_data, key=str.casefold):
        sheet_name = _unique_sheet_name(server, set(workbook.sheetnames))
        sheet_names[server] = sheet_name
        _build_server_sheet(workbook.create_sheet(sheet_name), server, week_start, week_end, hourly_data[server])

    _build_summary_sheet(workbook.create_sheet("SUMMARY"), week_start, week_end, sheet_names)
    workbook.active = 0

    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    temporary_path = f"{report_path}.{uuid.uuid4().hex}.tmp"
    try:
        workbook.save(temporary_path)
        os.replace(temporary_path, report_path)
    except Exception:
        if os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                SYSTEM_LOGGER.warning("Could not remove temporary weekly report %s", temporary_path)
        raise
    return report_path


def weekly_report_email_sent(week_end):
    marker_path = _email_marker_path(week_end)
    return os.path.isfile(marker_path)


def send_weekly_report_email(report_path, week_start, week_end):
    """Email a generated report using the configured SMTP connection."""
    email_cfg = load_weekly_report_email()
    smtp_server = str(email_cfg.get("smtp_server", "")).strip()
    recipients_value = email_cfg.get("to_addresses", [])
    if isinstance(recipients_value, str):
        recipients = [address.strip() for address in recipients_value.split(",") if address.strip()]
    elif isinstance(recipients_value, (list, tuple, set)):
        recipients = [str(address).strip() for address in recipients_value if str(address).strip()]
    else:
        recipients = []
    if not smtp_server:
        raise ValueError("SMTP server is not configured for the weekly report")
    if not recipients:
        raise ValueError("No email recipients are configured for the weekly report")
    if not os.path.isfile(report_path):
        raise FileNotFoundError(f"Weekly report workbook not found: {report_path}")

    username = str(email_cfg.get("username", "")).strip()
    password = str(email_cfg.get("password", "")).strip()
    from_address = str(email_cfg.get("from_address", "")).strip() or username or "alerts@localhost"
    port = int(email_cfg.get("port", 587) or 587)
    use_tls = bool(email_cfg.get("use_tls", True))

    message = EmailMessage()
    message["Subject"] = (
        f"Weekly IBM i ASP/CPU Report - "
        f"{week_start:%Y-%m-%d} to {week_end:%Y-%m-%d}"
    )
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Hello Team,\n\n"
        f"Attached is the weekly IBM i ASP/CPU report for "
        f"{week_start:%B %d, %Y} through {week_end:%B %d, %Y}.\n\n"
        "This report was generated automatically by the IBM i Monitoring Dashboard."
    )
    with open(report_path, "rb") as report_file:
        message.add_attachment(
            report_file.read(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=os.path.basename(report_path),
        )

    with smtplib.SMTP(smtp_server, port, timeout=15) as smtp:
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)
    return True


def mark_weekly_report_email_sent(week_end, report_path):
    marker_path = _email_marker_path(week_end)
    saved = safe_json_save(
        marker_path,
        {
            "week_ending": week_end.isoformat(),
            "report_path": os.path.abspath(report_path),
            "emailed_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    if not saved:
        raise OSError(f"Could not persist weekly report email status: {marker_path}")


def _email_marker_path(week_end):
    return os.path.join(
        get_app_data_dir(),
        "weekly_report_email_sent",
        f"{week_end:%Y-%m-%d}.json",
    )


def _validate_week(week_start, week_end):
    if isinstance(week_start, datetime):
        week_start = week_start.date()
    if isinstance(week_end, datetime):
        week_end = week_end.date()
    if not isinstance(week_start, date) or not isinstance(week_end, date):
        raise TypeError("Weekly report dates must be date objects")
    if week_start.weekday() != 5 or week_end.weekday() != 4:
        raise ValueError("Weekly report period must start on Saturday and end on Friday")
    if week_end - week_start != timedelta(days=6):
        raise ValueError("Weekly report period must contain exactly seven days")
    return week_start, week_end


def _read_weekly_hourly_data(week_start, week_end):
    measurements = defaultdict(list)
    seen = set()
    weekly_records = []

    for day_offset in range(7):
        current_date = week_start + timedelta(days=day_offset)
        log_dir = get_monthly_logs_dir_for(current_date.strftime("%Y-%m"))
        prefix = f"lpar_history_{current_date:%Y-%m-%d}"
        try:
            filenames = sorted(
                filename
                for filename in os.listdir(log_dir)
                if filename.startswith(prefix) and filename.endswith(".json")
            )
        except FileNotFoundError:
            continue

        for filename in filenames:
            file_path = os.path.join(log_dir, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                SYSTEM_LOGGER.warning("Skipping unreadable weekly report log %s: %s", file_path, exc)
                continue

            batches = payload if isinstance(payload, list) else [payload]
            for batch in batches:
                if not isinstance(batch, dict):
                    continue
                records = batch.get("records", [batch])
                if isinstance(records, dict):
                    records = [records]
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    timestamp = str(record.get("timestamp") or batch.get("timestamp") or "")
                    try:
                        recorded_at = datetime.fromisoformat(timestamp)
                    except ValueError:
                        continue
                    if not week_start <= recorded_at.date() <= week_end:
                        continue
                    weekly_records.append((recorded_at, record))

    hostnames_by_ip = defaultdict(set)
    for _, record in weekly_records:
        hostname = _record_server_name(record)
        if not hostname:
            continue
        for alias in _record_ip_aliases(record):
            hostnames_by_ip[alias].add(hostname)

    for recorded_at, record in weekly_records:
        server = _record_server_name(record)
        if not server:
            mapped_hostnames = {
                hostname
                for alias in _record_ip_aliases(record)
                for hostname in hostnames_by_ip.get(alias, ())
            }
            if len(mapped_hostnames) == 1:
                server = mapped_hostnames.pop()
        if not server:
            continue

        signature = (
            recorded_at.isoformat(),
            server.casefold(),
            record.get("asp"),
            record.get("cpu"),
        )
        if signature in seen:
            continue
        seen.add(signature)

        day_hour = (recorded_at.date(), recorded_at.hour)
        for metric in _METRICS:
            value = record.get(metric)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                measurements[(server, metric, day_hour)].append(float(value))

    hourly_data = defaultdict(lambda: {metric: {} for metric in _METRICS})
    for (server, metric, day_hour), values in measurements.items():
        hourly_data[server][metric][day_hour] = sum(values) / len(values)
    return dict(hourly_data)


def _record_server_name(record):
    for field in ("host_name", "server_name", "lpar", "server", "config_key"):
        value = str(record.get(field) or "").strip()
        if value and not _is_ip_address(value):
            return value.upper()
    return ""


def _record_ip_aliases(record):
    aliases = set()
    for field in ("host_name", "server_name", "lpar", "server", "config_key", "ip"):
        value = str(record.get(field) or "").strip()
        if _is_ip_address(value):
            aliases.add(str(ipaddress.ip_address(value)))
    return aliases


def _is_ip_address(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _unique_sheet_name(server, existing_names):
    base = re.sub(r"[\\/*?:\[\]]", "_", server).strip("'") or "UNKNOWN"
    base = base[:31]
    candidate = base
    suffix = 2
    while candidate.casefold() in {name.casefold() for name in existing_names}:
        suffix_text = f"_{suffix}"
        candidate = f"{base[:31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _build_server_sheet(sheet, server, week_start, week_end, data):
    sheet.sheet_view.showGridLines = True
    sheet.merge_cells("A1:AA1")
    sheet["A1"] = f"IBM i Performance & Storage Matrix Log - {server}"
    sheet["A1"].font = _TITLE_FONT
    sheet.merge_cells("A2:AA2")
    sheet["A2"] = (
        f"Weekly Hourly Operations Matrix: {week_start:%b %d, %Y} "
        f"({week_start:%a}) - {week_end:%b %d, %Y} ({week_end:%a})"
    )

    _write_metric_table(
        sheet,
        "ASP Storage Utilization (%) Matrix",
        data["asp"],
        week_start,
        heading_row=4,
        header_row=5,
        data_start_row=6,
        summary_row=13,
        summary_label="Weekly Avg",
        daily_summary="avg",
        weekly_summary="avg",
    )
    _write_metric_table(
        sheet,
        "CPU Utilization (%) Matrix",
        data["cpu"],
        week_start,
        heading_row=16,
        header_row=17,
        data_start_row=18,
        summary_row=25,
        summary_label="Weekly Peak",
        daily_summary="max",
        weekly_summary="max",
    )
    sheet.freeze_panes = "C6"
    sheet.column_dimensions["A"].width = 10
    sheet.column_dimensions["B"].width = 15
    for column in range(3, 27):
        sheet.column_dimensions[get_column_letter(column)].width = 9
    sheet.column_dimensions["AA"].width = 14


def _write_metric_table(
    sheet,
    title,
    hourly_data,
    week_start,
    heading_row,
    header_row,
    data_start_row,
    summary_row,
    summary_label,
    daily_summary,
    weekly_summary,
):
    sheet.cell(heading_row, 1, f"{1 if daily_summary == 'avg' else 2}. {title}").font = _SECTION_FONT
    headers = ["Month", "Date", *[f"{hour}:00" for hour in _HOURS], "Daily Avg" if daily_summary == "avg" else "Daily Peak"]
    for column, value in enumerate(headers, 1):
        cell = sheet.cell(header_row, column, value)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
        cell.border = _THIN_BORDER

    for day_offset in range(7):
        current_date = week_start + timedelta(days=day_offset)
        row = data_start_row + day_offset
        sheet.cell(row, 1, current_date.month)
        sheet.cell(row, 2, current_date.strftime("%d-%b-%y"))
        for hour in _HOURS:
            cell = sheet.cell(row, hour + 3)
            value = hourly_data.get((current_date, hour))
            if value is not None:
                cell.value = value / 100.0
                cell.number_format = "0.00%"
            cell.border = _THIN_BORDER
            cell.font = _DATA_FONT
            cell.alignment = Alignment(horizontal="center")
        summary_cell = sheet.cell(row, 27)
        summary_function = "AVERAGE" if daily_summary == "avg" else "MAX"
        summary_cell.value = f'=IFERROR({summary_function}(C{row}:Z{row}),"")'
        summary_cell.number_format = "0.00%"
        summary_cell.font = _BOLD_FONT
        summary_cell.border = _THIN_BORDER
        for column in (1, 2):
            sheet.cell(row, column).border = _THIN_BORDER
            sheet.cell(row, column).font = _DATA_FONT

    sheet.cell(summary_row, 2, summary_label).font = _BOLD_FONT
    summary_function = "AVERAGE" if weekly_summary == "avg" else "MAX"
    for column in range(3, 27):
        letter = get_column_letter(column)
        cell = sheet.cell(summary_row, column)
        cell.value = f'=IFERROR({summary_function}({letter}{data_start_row}:{letter}{data_start_row + 6}),"")'
        cell.number_format = "0.00%"
        cell.font = _BOLD_FONT
        cell.border = _THIN_BORDER
    final_cell = sheet.cell(summary_row, 27)
    final_cell.value = (
        f'=IFERROR({summary_function}(AA{data_start_row}:AA{data_start_row + 6}),"")'
    )
    final_cell.number_format = "0.00%"
    final_cell.font = _BOLD_FONT
    final_cell.border = _THIN_BORDER


def _build_summary_sheet(sheet, week_start, week_end, sheet_names):
    server_names = list(sheet_names)
    last_column = max(3 + len(server_names), 4)
    last_letter = get_column_letter(last_column)
    sheet.sheet_view.showGridLines = True
    sheet.merge_cells(f"A1:{last_letter}1")
    sheet["A1"] = "Consolidated System Performance & Storage Summary - All LPARs"
    sheet["A1"].font = _TITLE_FONT
    sheet.merge_cells(f"A2:{last_letter}2")
    sheet["A2"] = (
        f"Period: {week_start:%b %d, %Y} ({week_start:%A}) - "
        f"{week_end:%b %d, %Y} ({week_end:%A})"
    )

    for heading_row, header_row, data_start_row, title, metric, detail_row in (
        (4, 5, 6, "1. ASP Storage Utilization (%) Across All LPARs", "asp", 6),
        (16, 17, 18, "2. Peak CPU Utilization (%) Across All LPARs", "cpu", 18),
    ):
        sheet.cell(heading_row, 1, title).font = _SECTION_FONT
        headers = ["Month", "Date", "Day", *server_names]
        for column, value in enumerate(headers, 1):
            cell = sheet.cell(header_row, column, value)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = Alignment(horizontal="center")
            cell.border = _THIN_BORDER
        for day_offset in range(7):
            current_date = week_start + timedelta(days=day_offset)
            row = data_start_row + day_offset
            detail_data_row = detail_row + day_offset
            sheet.cell(row, 1, current_date.month)
            sheet.cell(row, 2, current_date.strftime("%d-%b-%y"))
            sheet.cell(row, 3, current_date.strftime("%A"))
            for server_index, server in enumerate(server_names, start=4):
                cell = sheet.cell(row, server_index)
                sheet_name = sheet_names[server]
                cell.value = f'=IFERROR(\'{sheet_name}\'!AA{detail_data_row},"")'
                cell.number_format = "0.00%"
                cell.border = _THIN_BORDER
                cell.font = _DATA_FONT
            for column in range(1, 4):
                sheet.cell(row, column).border = _THIN_BORDER
                sheet.cell(row, column).font = _DATA_FONT

    sheet.freeze_panes = "D6"
    sheet.column_dimensions["A"].width = 10
    sheet.column_dimensions["B"].width = 15
    sheet.column_dimensions["C"].width = 15
    for column in range(4, last_column + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 14
