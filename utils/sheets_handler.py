import gspread
from google.oauth2.service_account import Credentials
import config
import time
import logging

class GoogleSheetsHandler:
    def __init__(self, credentials_path, spreadsheet_id):
        self.credentials_path = credentials_path
        self.spreadsheet_id = spreadsheet_id
        self.scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self._retry_on_failure(self._authenticate)

    def _authenticate(self):
        self.credentials = Credentials.from_service_account_file(
            self.credentials_path, scopes=self.scopes
        )
        self.client = gspread.authorize(self.credentials)
        self.spreadsheet = self.client.open_by_key(self.spreadsheet_id)
        
        # Main Sheet
        self.sheet = self.spreadsheet.get_worksheet(0)
        
        # Logs Sheet
        try:
            self.log_sheet = self.spreadsheet.worksheet(config.LOGS_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            self.log_sheet = self.spreadsheet.add_worksheet(title=config.LOGS_SHEET_NAME, rows="1000", cols="10")
            self.log_sheet.append_row(config.LOG_COLUMNS)


    def _retry_on_failure(self, func, *args, **kwargs):
        """Retries a function call upon connection failures."""
        max_retries = 3
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if i == max_retries - 1:
                    raise e
                logging.warning(f"Sheet operation failed (attempt {i+1}): {e}. Retrying in 2s...")
                time.sleep(2)
                try:
                    self._authenticate() # Re-auth on failure
                except: pass

    def get_all_records(self):
        return self._retry_on_failure(self._get_all_records_internal)

    def _get_all_records_internal(self):
        # We use FORMULA to extract URLs from HYPERLINK functions
        data = self.sheet.get_all_values(value_render_option="FORMULA")
        if not data:
            return []
            
        headers = data[0]
        rows = data[1:]
        
        records = []
        for row in rows:
            record = {}
            for i, header in enumerate(headers):
                if i < len(row):
                    record[header] = row[i]
                else:
                    record[header] = ""
            
            # Clean numeric fields
            for key in [config.COL_PURCHASE_COST, config.COL_SITE_PRICE, config.COL_TOROB_PRICE, config.COL_SECOND_TOROB_PRICE]:
                if key in record:
                    val = str(record[key]).replace(',', '').strip()
                    try:
                        # Extract number if it's a formula like =12000
                        if val.startswith('='):
                            val = val[1:]
                        record[key] = int(float(val)) if val else 0
                    except (ValueError, TypeError):
                        record[key] = 0
            records.append(record)
        return records

    def update_cell(self, row, col_name, value):
        return self._retry_on_failure(self._update_cell_internal, row, col_name, value)

    def _update_cell_internal(self, row, col_name, value):
        col = self._find_column_index_internal(col_name) 
        if col:
            self.sheet.update_cell(row, col, value, value_input_option="USER_ENTERED")
        else:
            logging.error(f"Sheet Error: Column '{col_name}' not found!")

    def update_cell_by_index(self, row, col_idx, value):
        """Updates a cell using numeric column index (1-based)."""
        return self._retry_on_failure(self.sheet.update_cell, row, col_idx, value, value_input_option="USER_ENTERED")

    def find_column_index(self, column_name):
        return self._retry_on_failure(self._find_column_index_internal, column_name)

    def _find_column_index_internal(self, column_name):
        headers = self.sheet.row_values(1)
        try:
            return headers.index(column_name) + 1
        except ValueError:
            logging.error(f"Header '{column_name}' not found in: {headers}")
            return None

    def update_row(self, row_index, data_dict):
        """
        Updates a specific row. 
        Guarantees alignment by building the values list based on SHEET_COLUMNS.
        """
        # Build the list of values in the EXACT order defined in config.SHEET_COLUMNS
        values = []
        for header in config.SHEET_COLUMNS:
            values.append(data_dict.get(header, ""))
        
        # Determine the range (e.g., A2:H2)
        end_col = gspread.utils.rowcol_to_a1(row_index, len(config.SHEET_COLUMNS))
        range_label = f"A{row_index}:{end_col}"
        
        self.sheet.update(range_label, [values], value_input_option="USER_ENTERED")

    def append_row(self, data_dict):
        """Appends a new row while guaranteeing column alignment."""
        values = []
        for header in config.SHEET_COLUMNS:
            values.append(data_dict.get(header, ""))
        
        # Use simple append_row for guaranteed new row insertion
        return self._retry_on_failure(self.sheet.append_row, values, value_input_option="USER_ENTERED")

    def delete_row(self, row_index):
        """Deletes a specific row by index (1-based)."""
        return self._retry_on_failure(self.sheet.delete_rows, row_index)

    def format_hyperlink(self, url, label):
        if not url:
            return ""
        return f'=HYPERLINK("{url}", "{label}")'

    def color_row(self, row_index, color_rgb):
        """
        Colors a row. color_rgb should be a dict like {'red': 1.0, 'green': 0.0, 'blue': 0.0}
        """
        try:
            fmt = {
                "backgroundColor": color_rgb,
                "textFormat": {"bold": True}
            }
            # Determine range
            end_col_idx = len(config.SHEET_COLUMNS)
            self.sheet.format(f"A{row_index}:{gspread.utils.rowcol_to_a1(row_index, end_col_idx)}", fmt)
        except Exception as e:
            logging.error(f"Error coloring row {row_index}: {e}")

    def ensure_headers(self, required_headers):
        """Checks if headers exist, adds them if missing."""
        try:
            headers = self.sheet.row_values(1)
            if not headers:
                logging.info(f"Setting headers: {required_headers}")
                self.sheet.insert_row(required_headers, 1)
                return

            missing = False
            for h in required_headers:
                if h not in headers:
                    missing = True
                    break
            
            if missing:
                logging.info("Headers mismatch. Updating to: %s", required_headers)
                # Overwrite first row with correct headers
                self.sheet.update("A1", [required_headers])
        except Exception as e:
            logging.error(f"Error ensuring headers: {e}")
            raise e

    def append_log(self, admin_name, date_str, time_str, product_name, old_price, new_price):
        """Appends a new entry to the Logs sheet."""
        values = [admin_name, date_str, time_str, product_name, old_price, new_price]
        return self._retry_on_failure(self.log_sheet.append_row, values, value_input_option="USER_ENTERED")

    def get_recent_logs(self, limit=10):
        """Fetches the last N records from the Logs sheet."""
        return self._retry_on_failure(self._get_recent_logs_internal, limit)

    def _get_recent_logs_internal(self, limit):
        all_logs = self.log_sheet.get_all_records()
        if not all_logs:
            return []
        # Return last N logs in reverse order (newest first)
        return all_logs[-limit:][::-1]

