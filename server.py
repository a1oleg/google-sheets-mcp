from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP


ENV_PATH = Path(__file__).resolve().parent / '.env'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly',
]


def load_settings() -> dict[str, str]:
    load_dotenv(ENV_PATH)

    values = {
        'type': os.getenv('GOOGLE_TYPE') or 'service_account',
        'project_id': os.getenv('GOOGLE_PROJECT_ID'),
        'private_key_id': os.getenv('GOOGLE_PRIVATE_KEY_ID'),
        'private_key': os.getenv('GOOGLE_PRIVATE_KEY'),
        'client_email': os.getenv('GOOGLE_CLIENT_EMAIL'),
        'client_id': os.getenv('GOOGLE_CLIENT_ID'),
        'token_uri': os.getenv('GOOGLE_TOKEN_URI') or 'https://oauth2.googleapis.com/token',
        'auth_uri': os.getenv('GOOGLE_AUTH_URI') or 'https://accounts.google.com/o/oauth2/auth',
        'auth_provider_x509_cert_url': os.getenv('GOOGLE_AUTH_PROVIDER_X509_CERT_URL') or 'https://www.googleapis.com/oauth2/v1/certs',
        'client_x509_cert_url': os.getenv('GOOGLE_CLIENT_X509_CERT_URL'),
        'universe_domain': os.getenv('GOOGLE_UNIVERSE_DOMAIN') or 'googleapis.com',
    }

    missing = [
        name
        for name in [
            'project_id',
            'private_key_id',
            'private_key',
            'client_email',
            'client_id',
            'token_uri',
        ]
        if not values[name]
    ]
    if missing:
        raise RuntimeError(f"Missing required Google service account settings: {', '.join(missing)}")

    values['private_key'] = values['private_key'].replace('\\n', '\n')
    return values


def build_credentials() -> Credentials:
    settings = load_settings()
    info = {
        'type': settings['type'],
        'project_id': settings['project_id'],
        'private_key_id': settings['private_key_id'],
        'private_key': settings['private_key'],
        'client_email': settings['client_email'],
        'client_id': settings['client_id'],
        'auth_uri': settings['auth_uri'],
        'token_uri': settings['token_uri'],
        'auth_provider_x509_cert_url': settings['auth_provider_x509_cert_url'],
        'universe_domain': settings['universe_domain'],
    }
    if settings['client_x509_cert_url']:
        info['client_x509_cert_url'] = settings['client_x509_cert_url']

    return Credentials.from_service_account_info(info, scopes=SCOPES)


class LazyGoogleService:
    def __init__(self, api_name: str, api_version: str) -> None:
        self.api_name = api_name
        self.api_version = api_version
        self.service: Any | None = None

    def __getattr__(self, name: str) -> Any:
        if self.service is None:
            self.service = build(
                self.api_name,
                self.api_version,
                credentials=build_credentials(),
                cache_discovery=False,
            )
        return getattr(self.service, name)


SHEETS_SERVICE = LazyGoogleService('sheets', 'v4')
DRIVE_SERVICE = LazyGoogleService('drive', 'v3')

DEFAULT_MAX_WRITE_CELLS = 10_000
DEFAULT_MAX_INSERT_ROWS = 1_000

app = FastMCP(
    name='google-sheets-mcp',
    instructions='Read and update Google Sheets data through a service-account-backed MCP server.',
)


def parse_json_object(value: str, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f'{field_name} must be valid JSON.') from error

    if not isinstance(parsed, dict):
        raise ValueError(f'{field_name} must decode to an object.')

    return parsed


def parse_json_array(value: str, field_name: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f'{field_name} must be valid JSON.') from error

    if not isinstance(parsed, list):
        raise ValueError(f'{field_name} must decode to an array.')

    return parsed


def configured_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, '').strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f'{name} must be a positive integer.') from error
    if value < 1:
        raise RuntimeError(f'{name} must be a positive integer.')
    return value


def allowed_spreadsheet_ids() -> set[str]:
    return {
        item.strip()
        for item in os.getenv('GOOGLE_ALLOWED_SPREADSHEET_IDS', '').split(',')
        if item.strip()
    }


def ensure_spreadsheet_allowed(spreadsheet_id: str) -> None:
    allowed = allowed_spreadsheet_ids()
    if allowed and spreadsheet_id not in allowed:
        raise PermissionError('The requested spreadsheet is not in GOOGLE_ALLOWED_SPREADSHEET_IDS.')


def get_allowlisted_files(limit: int, fields: str) -> list[dict[str, Any]] | None:
    allowed = sorted(allowed_spreadsheet_ids())
    if not allowed:
        return None

    files: list[dict[str, Any]] = []
    for spreadsheet_id in allowed[:limit]:
        files.append(
            DRIVE_SERVICE.files().get(
                fileId=spreadsheet_id,
                fields=fields,
                supportsAllDrives=True,
            ).execute(),
        )
    return files


def validate_values(values: list[Any], field_name: str = 'values') -> tuple[int, int, int]:
    if not isinstance(values, list):
        raise ValueError(f'{field_name} must be a two-dimensional array.')

    row_count = len(values)
    column_count = 0
    cell_count = 0
    for row_index, row in enumerate(values):
        if not isinstance(row, list):
            raise ValueError(f'{field_name}[{row_index}] must be an array.')
        column_count = max(column_count, len(row))
        cell_count += len(row)

    max_cells = configured_positive_int('GOOGLE_MAX_WRITE_CELLS', DEFAULT_MAX_WRITE_CELLS)
    if cell_count > max_cells:
        raise ValueError(f'{field_name} contains {cell_count} cells; the configured limit is {max_cells}.')

    return row_count, column_count, cell_count


def audit_mutation(operation: str, details: dict[str, Any]) -> None:
    event = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'operation': operation,
        **details,
    }
    print(json.dumps(event, ensure_ascii=True, separators=(',', ':')), file=sys.stderr, flush=True)


def quote_sheet_title(title: str) -> str:
    return f"'{title.replace("'", "''")}'"


def list_sheet_metadata(spreadsheet_id: str) -> list[dict[str, Any]]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    response = SHEETS_SERVICE.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields='sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))',
    ).execute()
    sheets = response.get('sheets', [])
    ordered = sorted(sheets, key=lambda item: item.get('properties', {}).get('index', 0))

    return [
        {
            'sheetId': sheet['properties']['sheetId'],
            'title': sheet['properties']['title'],
            'index': sheet['properties'].get('index', 0),
            'rowCount': sheet['properties'].get('gridProperties', {}).get('rowCount'),
            'columnCount': sheet['properties'].get('gridProperties', {}).get('columnCount'),
        }
        for sheet in ordered
    ]


def resolve_notation_to_range(spreadsheet_id: str, notation: str) -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    notation_text = notation.strip()
    if not notation_text:
        raise ValueError('notation must not be empty.')

    stable_match = re.fullmatch(r'@(\d+)(?:!(.*))?', notation_text, flags=re.DOTALL)
    if stable_match:
        requested_sheet_id = int(stable_match.group(1))
        sheets = list_sheet_metadata(spreadsheet_id)
        selected_sheet = next(
            (sheet for sheet in sheets if sheet['sheetId'] == requested_sheet_id),
            None,
        )
        if selected_sheet is None:
            raise ValueError(f'No sheet exists with sheetId {requested_sheet_id}.')

        suffix = stable_match.group(2) or ''
        quoted_title = quote_sheet_title(selected_sheet['title'])
        resolved_range = quoted_title if not suffix else f'{quoted_title}!{suffix}'
        return {
            'notation': notation_text,
            'resolvedRange': resolved_range,
            'sheetTitle': selected_sheet['title'],
            'sheetIndex': selected_sheet['index'],
            'sheetId': selected_sheet['sheetId'],
            'mode': 'sheet-id-notation',
        }

    if not notation_text.startswith('!'):
        return {
            'notation': notation_text,
            'resolvedRange': notation_text,
            'mode': 'explicit',
        }

    bang_count = 0
    for character in notation_text:
        if character != '!':
            break
        bang_count += 1

    sheets = list_sheet_metadata(spreadsheet_id)
    index = bang_count - 1
    if index < 0 or index >= len(sheets):
        raise ValueError(f'No sheet exists at position {bang_count}.')

    selected_sheet = sheets[index]
    suffix = notation_text[bang_count:]
    quoted_title = quote_sheet_title(selected_sheet['title'])
    resolved_range = quoted_title if not suffix else f'{quoted_title}!{suffix}'

    return {
        'notation': notation_text,
        'resolvedRange': resolved_range,
        'sheetTitle': selected_sheet['title'],
        'sheetIndex': selected_sheet['index'],
        'sheetId': selected_sheet['sheetId'],
        'mode': 'bang-notation',
    }


def read_range(spreadsheet_id: str, range_a1: str, major_dimension: str = 'ROWS') -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    response = SHEETS_SERVICE.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        majorDimension=major_dimension,
    ).execute()

    return {
        'spreadsheetId': spreadsheet_id,
        'range': response.get('range', range_a1),
        'majorDimension': response.get('majorDimension', major_dimension),
        'values': response.get('values', []),
    }


@app.tool(description='Verify Google Sheets and Drive access using the configured service account.', structured_output=False)
def check_connection() -> dict[str, Any]:
    visible_files = get_allowlisted_files(5, 'id,name')
    if visible_files is None:
        response = DRIVE_SERVICE.files().list(
            q="mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            pageSize=5,
            fields='files(id,name)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        visible_files = response.get('files', [])

    return {
        'status': 'ok',
        'spreadsheetCountSample': len(visible_files),
        'spreadsheets': visible_files,
        'allowlistEnabled': bool(allowed_spreadsheet_ids()),
    }


@app.tool(description='List spreadsheets visible to the service account. Optional query uses Drive search syntax.', structured_output=False)
def list_spreadsheets(limit: int = 20, query: str = '') -> dict[str, Any]:
    page_size = max(1, min(limit, 100))
    query_text = query.strip() or "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    file_fields = 'id,name,webViewLink,createdTime,modifiedTime'
    visible_files = get_allowlisted_files(page_size, file_fields)
    if visible_files is None:
        response = DRIVE_SERVICE.files().list(
            q=query_text,
            pageSize=page_size,
            fields=f'files({file_fields})',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        visible_files = response.get('files', [])

    return {
        'files': visible_files,
        'count': len(visible_files),
        'query': query_text if not allowed_spreadsheet_ids() else 'GOOGLE_ALLOWED_SPREADSHEET_IDS',
        'allowlistEnabled': bool(allowed_spreadsheet_ids()),
    }


@app.tool(description='List sheets in a spreadsheet in their current order.', structured_output=False)
def list_sheets(spreadsheet_id: str) -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    return {
        'spreadsheetId': spreadsheet_id,
        'sheets': list_sheet_metadata(spreadsheet_id),
    }


@app.tool(description='Resolve explicit A1 notation, positional notation like !!A1:G5, or stable sheet-id notation like @123!A1:G5.', structured_output=False)
def resolve_chat_notation(spreadsheet_id: str, notation: str) -> dict[str, Any]:
    return resolve_notation_to_range(spreadsheet_id, notation)


@app.tool(description='Read values from a spreadsheet using standard A1 notation.', structured_output=False)
def get_sheet_data(spreadsheet_id: str, range_a1: str, major_dimension: str = 'ROWS') -> dict[str, Any]:
    return read_range(spreadsheet_id, range_a1, major_dimension=major_dimension)


@app.tool(description='Read values using explicit A1 notation, positional notation like !!13:13, or stable sheet-id notation like @123!A1:Z3.', structured_output=False)
def get_sheet_data_by_notation(spreadsheet_id: str, notation: str, major_dimension: str = 'ROWS') -> dict[str, Any]:
    resolved = resolve_notation_to_range(spreadsheet_id, notation)
    payload = read_range(spreadsheet_id, resolved['resolvedRange'], major_dimension=major_dimension)
    payload['notation'] = notation
    payload['resolvedRange'] = resolved['resolvedRange']
    if 'sheetTitle' in resolved:
        payload['sheetTitle'] = resolved['sheetTitle']
    return payload


@app.tool(description='Update a target A1 range. values_json must be a JSON 2D array, for example [["A","B"],[1,2]].', structured_output=False)
def update_cells(
    spreadsheet_id: str,
    range_a1: str,
    values_json: str,
    value_input_option: str = 'USER_ENTERED',
    dry_run: bool = False,
    request_id: str = '',
) -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    values = parse_json_array(values_json, 'values_json')
    row_count, column_count, cell_count = validate_values(values, 'values_json')

    audit_details = {
        'spreadsheetId': spreadsheet_id,
        'range': range_a1,
        'rows': row_count,
        'columns': column_count,
        'cells': cell_count,
        'dryRun': dry_run,
        **({'requestId': request_id} if request_id else {}),
    }
    audit_mutation('update_cells', audit_details)

    if dry_run:
        return {
            **audit_details,
            'status': 'dry-run',
        }

    response = SHEETS_SERVICE.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        valueInputOption=value_input_option,
        body={'values': values},
    ).execute()

    return {
        'spreadsheetId': spreadsheet_id,
        'range': response.get('updatedRange', range_a1),
        'updatedRows': response.get('updatedRows', 0),
        'updatedColumns': response.get('updatedColumns', 0),
        'updatedCells': response.get('updatedCells', 0),
    }


@app.tool(description='Batch update multiple A1 ranges. updates_json must be a JSON array of objects with range and values fields.', structured_output=False)
def batch_update_cells(
    spreadsheet_id: str,
    updates_json: str,
    value_input_option: str = 'USER_ENTERED',
    dry_run: bool = False,
    request_id: str = '',
) -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    updates = parse_json_array(updates_json, 'updates_json')
    normalized_updates: list[dict[str, Any]] = []
    total_cells = 0

    for index, update in enumerate(updates):
        if not isinstance(update, dict):
            raise ValueError(f'updates_json[{index}] must be an object.')
        range_a1 = update.get('range')
        values = update.get('values')
        if not isinstance(range_a1, str) or not range_a1.strip():
            raise ValueError(f'updates_json[{index}].range must be a non-empty string.')
        if not isinstance(values, list):
            raise ValueError(f'updates_json[{index}].values must be an array.')

        _, _, cell_count = validate_values(values, f'updates_json[{index}].values')
        total_cells += cell_count

        normalized_updates.append({
            'range': range_a1,
            'values': values,
        })

    max_cells = configured_positive_int('GOOGLE_MAX_WRITE_CELLS', DEFAULT_MAX_WRITE_CELLS)
    if total_cells > max_cells:
        raise ValueError(f'updates_json contains {total_cells} cells; the configured limit is {max_cells}.')

    audit_details = {
        'spreadsheetId': spreadsheet_id,
        'ranges': len(normalized_updates),
        'cells': total_cells,
        'dryRun': dry_run,
        **({'requestId': request_id} if request_id else {}),
    }
    audit_mutation('batch_update_cells', audit_details)

    if dry_run:
        return {
            **audit_details,
            'status': 'dry-run',
            'updates': normalized_updates,
        }

    response = SHEETS_SERVICE.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            'valueInputOption': value_input_option,
            'data': normalized_updates,
        },
    ).execute()

    return {
        'spreadsheetId': spreadsheet_id,
        'totalUpdatedSheets': response.get('totalUpdatedSheets', 0),
        'totalUpdatedRows': response.get('totalUpdatedRows', 0),
        'totalUpdatedColumns': response.get('totalUpdatedColumns', 0),
        'totalUpdatedCells': response.get('totalUpdatedCells', 0),
        'responses': response.get('responses', []),
    }


@app.tool(description='Insert empty rows into a sheet before start_index. Indices are zero-based Google Sheets row indices. Supports dry-run.', structured_output=False)
def insert_rows(
    spreadsheet_id: str,
    sheet_id: int,
    start_index: int,
    row_count: int = 1,
    dry_run: bool = False,
    request_id: str = '',
) -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    if start_index < 0:
        raise ValueError('start_index must be zero or greater.')
    if row_count < 1:
        raise ValueError('row_count must be at least 1.')
    max_rows = configured_positive_int('GOOGLE_MAX_INSERT_ROWS', DEFAULT_MAX_INSERT_ROWS)
    if row_count > max_rows:
        raise ValueError(f'row_count exceeds the configured limit of {max_rows}.')

    end_index = start_index + row_count
    audit_details = {
        'spreadsheetId': spreadsheet_id,
        'sheetId': sheet_id,
        'startIndex': start_index,
        'rowCount': row_count,
        'dryRun': dry_run,
        **({'requestId': request_id} if request_id else {}),
    }
    audit_mutation('insert_rows', audit_details)

    if dry_run:
        return {
            **audit_details,
            'status': 'dry-run',
        }

    SHEETS_SERVICE.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            'requests': [
                {
                    'insertDimension': {
                        'range': {
                            'sheetId': sheet_id,
                            'dimension': 'ROWS',
                            'startIndex': start_index,
                            'endIndex': end_index,
                        },
                        'inheritFromBefore': start_index > 0,
                    },
                },
            ],
        },
    ).execute()

    return {
        'spreadsheetId': spreadsheet_id,
        'sheetId': sheet_id,
        'startIndex': start_index,
        'rowCount': row_count,
        'status': 'ok',
    }


@app.tool(description='Get lightweight spreadsheet metadata including title and ordered sheet list.', structured_output=False)
def get_spreadsheet_info(spreadsheet_id: str) -> dict[str, Any]:
    ensure_spreadsheet_allowed(spreadsheet_id)
    response = SHEETS_SERVICE.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields='spreadsheetId,properties(title),sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))',
    ).execute()

    return {
        'spreadsheetId': response['spreadsheetId'],
        'title': response.get('properties', {}).get('title'),
        'sheets': list_sheet_metadata(spreadsheet_id),
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--check':
        print(json.dumps(check_connection(), ensure_ascii=True, indent=2))
        return 0

    app.run(transport='stdio')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
