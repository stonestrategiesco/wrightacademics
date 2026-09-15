"""
Monday.com GraphQL API client.

Uses the items_page / next_items_page pagination model (Monday API
2023-10+), which is required to reliably retrieve every item on a board —
older `items` fields on boards/columns are capped and must not be used here.
"""

import json
import logging
import time

import requests

logger = logging.getLogger("wright_sync.monday")

MONDAY_API_URL = "https://api.monday.com/v2"
PAGE_LIMIT = 100

_RETRYABLE_ERROR_MARKERS = (
    "complexity budget exhausted",
    "rate limit",
    "internal server error",
)


class MondayAPIError(RuntimeError):
    """Raised when a Monday.com API call fails permanently (after retries)."""


class MondayClient:
    def __init__(self, api_token, timeout=30, max_retries=5, retry_base_delay=1.0, session=None):
        if not api_token:
            raise ValueError("Monday API token is required")
        self.api_token = api_token
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.session = session or requests.Session()

    def _headers(self):
        return {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
        }

    def _execute(self, query, variables=None):
        """POST a GraphQL query/mutation with retries/backoff for transient failures."""
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    MONDAY_API_URL,
                    headers=self._headers(),
                    json={"query": query, "variables": variables or {}},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Monday request error (attempt %d/%d): %s", attempt, self.max_retries, exc)
                self._sleep_backoff(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = MondayAPIError(f"Monday returned HTTP {response.status_code}: {response.text[:500]}")
                logger.warning("Monday transient HTTP error %s (attempt %d/%d)", response.status_code, attempt, self.max_retries)
                self._sleep_backoff(attempt)
                continue

            if response.status_code != 200:
                raise MondayAPIError(f"Monday request failed with HTTP {response.status_code}: {response.text[:500]}")

            payload = response.json()
            if "errors" in payload and payload["errors"]:
                message = json.dumps(payload["errors"])[:800]
                if any(marker in message.lower() for marker in _RETRYABLE_ERROR_MARKERS):
                    last_error = MondayAPIError(f"Monday transient GraphQL error: {message}")
                    logger.warning("Monday transient GraphQL error (attempt %d/%d): %s", attempt, self.max_retries, message)
                    self._sleep_backoff(attempt)
                    continue
                raise MondayAPIError(f"Monday GraphQL error: {message}")

            return payload["data"]

        raise MondayAPIError(f"Monday request failed after {self.max_retries} attempts: {last_error}")

    def _sleep_backoff(self, attempt):
        if attempt < self.max_retries:
            time.sleep(self.retry_base_delay * (2 ** (attempt - 1)))

    def _iter_board_items(self, board_id, column_ids):
        """Yield every item on a board, fully paginated via items_page/next_items_page."""
        query = """
        query ($boardId: [ID!], $limit: Int, $columnIds: [String!]) {
          boards(ids: $boardId) {
            items_page(limit: $limit) {
              cursor
              items {
                id
                name
                column_values(ids: $columnIds) { id text value }
              }
            }
          }
        }
        """
        data = self._execute(query, {"boardId": [str(board_id)], "limit": PAGE_LIMIT, "columnIds": column_ids})
        boards = data.get("boards") or []
        if not boards:
            raise MondayAPIError(f"Monday board {board_id} not found or not accessible with this token")
        page = boards[0]["items_page"]
        for item in page["items"]:
            yield item
        cursor = page["cursor"]

        next_query = """
        query ($cursor: String!, $limit: Int, $columnIds: [String!]) {
          next_items_page(cursor: $cursor, limit: $limit) {
            cursor
            items {
              id
              name
              column_values(ids: $columnIds) { id text value }
            }
          }
        }
        """
        while cursor:
            data = self._execute(next_query, {"cursor": cursor, "limit": PAGE_LIMIT, "columnIds": column_ids})
            page = data["next_items_page"]
            for item in page["items"]:
                yield item
            cursor = page["cursor"]

    @staticmethod
    def _column_text(item, column_id):
        for cv in item.get("column_values", []):
            if cv["id"] == column_id:
                return cv.get("text") or ""
        return ""

    def get_existing_unique_ids(self, board_id, unique_id_column):
        """Return the set of every non-empty unique-key value already present
        on the Session Log board. This is the entire duplicate-prevention index."""
        ids = set()
        for item in self._iter_board_items(board_id, [unique_id_column]):
            value = self._column_text(item, unique_id_column)
            if value:
                ids.add(value)
        return ids

    def get_items(self, board_id, column_ids):
        """Read-only: return every item on a board with its name and the
        given columns' text values, as a list of
        {'item_id': ..., 'item_name': ..., 'columns': {col_id: text}}.
        Used for diagnostics that need more than one column per item (e.g.
        the dedup diagnostic); makes no writes of any kind."""
        results = []
        for item in self._iter_board_items(board_id, column_ids):
            columns = {column_id: self._column_text(item, column_id) for column_id in column_ids}
            results.append({"item_id": item["id"], "item_name": item.get("name"), "columns": columns})
        return results

    def get_board_columns(self, board_id):
        """Read-only: return every column defined on a board, as a list of
        {'id': ..., 'title': ..., 'type': ...}. A single query, no
        pagination needed (board schema, not items). Makes no writes of
        any kind - used to discover real column IDs before any write code
        is built against them."""
        query = """
        query ($boardId: [ID!]) {
          boards(ids: $boardId) {
            columns {
              id
              title
              type
            }
          }
        }
        """
        data = self._execute(query, {"boardId": [str(board_id)]})
        boards = data.get("boards") or []
        if not boards:
            raise MondayAPIError(f"Monday board {board_id} not found or not accessible with this token")
        return boards[0]["columns"]

    def get_student_lookup(self, board_id, teachworks_id_column):
        """Return {teachworks_student_id (str): monday_item_id (str)} for every
        Student item that has a Teachworks Student ID populated."""
        lookup = {}
        for item in self._iter_board_items(board_id, [teachworks_id_column]):
            tw_id = self._column_text(item, teachworks_id_column)
            if tw_id:
                lookup[tw_id] = item["id"]
        return lookup

    def create_session_item(self, board_id, group_id, item_name, column_values):
        """Create one Session Log item. `column_values` is a dict of
        {column_id: monday-formatted-value}; None values are omitted.
        Returns the new item's id (str)."""
        clean_values = {k: v for k, v in column_values.items() if v is not None}
        mutation = """
        mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON!) {
          create_item(board_id: $boardId, group_id: $groupId, item_name: $itemName, column_values: $columnValues) {
            id
          }
        }
        """
        data = self._execute(mutation, {
            "boardId": str(board_id),
            "groupId": group_id,
            "itemName": item_name,
            "columnValues": json.dumps(clean_values),
        })
        return data["create_item"]["id"]

    def connect_student(self, board_id, item_id, column_id, student_item_id):
        """Set a board_relation column on an existing item to point at one student item."""
        mutation = """
        mutation ($boardId: ID!, $itemId: ID!, $columnId: String!, $value: JSON!) {
          change_column_value(board_id: $boardId, item_id: $itemId, column_id: $columnId, value: $value) {
            id
          }
        }
        """
        value = json.dumps({"item_ids": [int(student_item_id)]})
        data = self._execute(mutation, {
            "boardId": str(board_id),
            "itemId": str(item_id),
            "columnId": column_id,
            "value": value,
        })
        return data["change_column_value"]["id"]
