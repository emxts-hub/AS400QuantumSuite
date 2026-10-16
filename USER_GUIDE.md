# AS400 Quantum Suite
## End-User Guide

This guide is for operators using the prebuilt Windows application. You do not need to install Python or run any source-code commands.

## 1. Before you start

Make sure you have:

- A Windows 10 or later workstation.
- Network access to the IBM i systems. Connect to your organization's VPN before using the app.
- The IBM i Access ODBC driver, or a compatible DB2 ODBC driver, installed on the workstation.
- IBM i host names or IP addresses, database names, and an IBM i user account.
- A writable folder for monitoring history. Use a shared OneDrive or SharePoint folder only if it is available to your Windows account.
- SMTP server details if you need email alerts.

The credential check also verifies the VPN network; this build expects the workstation's IPv4 address to start with `10.212`. Contact your administrator if you are unsure about VPN or driver setup.

## 2. Install and open the application

If you received the installer, run `AS400QuantumSuite-Setup.exe` and follow the prompts. Use the Start menu shortcut, or the desktop shortcut if you selected that option.

If you received the portable application folder instead, extract the entire folder and run `AS400QuantumSuite.exe` from inside it. Keep the executable and its neighboring files together; do not copy the `.exe` by itself to another folder.

At launch, the application checks its build expiration and whether the installed version meets the published minimum version. If the installed version is below the minimum, the app requires a supported build; use the update link shown or contact the person who provided the installer. To check for a newer release at any time, open **Settings & Credentials** > **Storage & System** and select **Check for Updates** under **Application Updates**. Select **Download Update** if a newer version is available.

## 3. Add your IBM i systems

1. Open **Settings & Credentials** and select **LPAR Configuration**.
2. Choose **+ Add LPAR** for each IBM i system you want to monitor.
3. Enter the system IP address or host name and database name.
4. If you want backup history, enter the system's daily, journal, and monthly backup job names.
5. Optionally configure:
   - **Expected Subsystems** as a comma-separated list of subsystem names.
   - **Monitored Ports** as comma-separated `Port:Name` entries, such as `443:HTTPS`.
   - **Web Apps** as comma-separated `Job:Port` entries, such as `MYAPP:8080`.
6. Select **Storage & System**. Choose a writable **Log Storage Root Folder**. Optionally use the separate **ASP Excel Workbook** and **CPU Excel Workbook** browse buttons to select the shared workbooks. Leave either field blank to use the default `BY - ASP YYYY.xlsx` or `BY - CPU YYYY.xlsx` inside the log root's year folder.
7. Click **Save Changes** to apply the system and storage configuration.

The expected subsystem, port, and web-app settings tell the dashboard what to check; only configure the items that matter in your environment.

## 4. Check your credentials

1. In **Settings & Credentials**, select **Credentials**.
2. Enter your IBM i username and password.
3. Connect to the required VPN, then click **Check Login Creds**.
4. Wait for the result. The app reports which configured systems accepted the credentials.
5. If you want the username and password remembered, select **Remember credentials**. The username preference is saved with the app settings, and the password is saved through the Windows keyring when available.

The **Start Auto-Refresh** button is enabled only when at least one system is configured and credential validation succeeds. If validation fails, confirm the VPN connection, host and database values, ODBC driver, account permissions, username, and password.

## 5. Configure optional email alerts

1. Open **Settings & Credentials** > **Email & Alerts**.
2. Enable **Enable email alerts** and enter the SMTP server, port, sender address, recipient address or addresses, and SMTP username/password.
3. Configure the ASP **Threshold %** and email cooldown. The configured threshold is the critical level; the warning level is normally two percentage points below it.
4. Click **Save Changes**, then **Send Test Email**.

Warnings appear in orange and do not sound. Critical ASP alerts appear in red and sound. Email alerts require valid SMTP settings and a server that accepts the configured sender, recipients, and credentials.

## 6. Monitor systems

1. Open **Live Monitor**.
2. Click **Start Auto-Refresh**.
3. Review each system card for connection status, CPU and ASP usage, active jobs, subsystem health, and configured service or web-app checks.
4. Select a card or its details to inspect additional system information.
5. Click **Stop Auto-Refresh** when you want to pause monitoring. Stop monitoring before changing settings.

The refresh interval can be selected in **Email & Alerts**. The application queries systems independently, so one unavailable system does not necessarily stop updates for the others.

## 7. View logs and monthly reports

Open **Logs & Analytics**:

- **System Logs** shows persisted monitoring history, including system status and detected issues. The page refreshes its history while visible.
- **Monthly ASP/CPU** shows historical capacity trends. Choose a month and select **Export to Excel** to save a report as an Excel workbook or CSV.
- Use **Generate Previous Month** when you need to create the prior month's report.

Daily monitoring history is saved as JSON under the selected log root, organized by year and month. The JSON history is saved locally before the app attempts to update shared Excel workbooks, so workbook export problems do not erase the monitoring history.

### Optional shared hourly Excel workbooks

If your organization uses hourly workbooks, select the ASP and CPU workbooks separately in **Settings & Credentials** > **Storage & System**, or leave the selectors blank and place `BY - ASP YYYY.xlsx` and `BY - CPU YYYY.xlsx` in the matching year folder under the selected log root. Each workbook must have a worksheet named for the IBM i host and the expected date/hour cells from the workbook template. If a selected workbook follows the standard annual filename and year-folder layout, the app uses the corresponding year's workbook. After all configured systems report for the hour, the app writes the readings.

Keep these workbooks closed in Excel while the application updates them. Missing or open workbooks, mismatched worksheet names, or missing date/hour cells are reported in **System Logs**. Ask your administrator to correct the workbook template or folder if these errors persist.

## 8. Review backup history

After credential validation, open **Backup Management**. Select a system and month to review available daily/weekly, monthly, and journal backup information, including times, durations, status, and averages. Backup data is queried from the IBM i system and depends on the configured backup job names.

## 9. Settings, backups, and privacy

- **Save Changes** stores configuration for the current Windows user. Settings are not automatically shared between different user accounts or computers.
- Use **Backup Configuration** and **Restore** under **Storage & System** to export or restore settings.
- A settings backup may contain credentials. Store it only in an approved, access-controlled location and share it only with authorized administrators.
- Monitoring history is stored in the configured log folder. If that location is unavailable, JSON history may fall back to the application's local logs folder.
- Older daily monitoring logs are subject to the application's 30-day retention policy. Keep separate archives if your organization needs longer retention.
- If you no longer have access to a configured shared folder, update the log root in **Settings & Credentials** > **Storage & System**.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| The app cannot connect to an IBM i system | Connect to the VPN, confirm the IBM i host and database name, check the ODBC driver, and validate the account credentials. |
| Credential check says the VPN is unavailable | Connect to the organization's VPN and contact your administrator if your workstation does not receive the expected `10.212` IPv4 address. |
| A system card shows a connection or query error | Check that the IBM i system is reachable and that the account has permission to run the required queries. Review **Settings & Credentials** > **System Logs**. |
| Email test or alerts fail | Confirm SMTP host, port, TLS option, sender/recipient addresses, account credentials, and network access to the mail server. |
| Hourly Excel values are not updated | Confirm the year folder and both workbook names are correct, workbooks are closed, and worksheets/date-hour cells match the required template. JSON history should still be available. |
| Backup Management is empty or incomplete | Validate credentials and confirm the configured backup job names and IBM i backup history. |
| The app says an update is required or the build has expired | Contact the person who supplied the installer for a supported build. |
| The update check cannot reach the version server | Confirm internet access and try **Check for Updates** again later. The manual check reads the published version metadata; it does not download or install the update. |
| A newer version is available | Select **Download Update** to open the release page, then follow your organization's installation instructions or contact the person who supplied the app. |

For problems that remain unresolved, provide your administrator with the system name, approximate time, and relevant message from **System Logs**. Do not send passwords or settings backups through an unapproved channel.


[🏠 Back to README](./README.md)
