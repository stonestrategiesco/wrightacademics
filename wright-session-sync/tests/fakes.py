"""In-memory fake clients used by the test suite. These implement just enough
of the real TeachworksClient / MondayClient interfaces for sync.run_sync to
be tested without any network access."""

import config
from teachworks import TeachworksClient


class FakeTeachworksClient(TeachworksClient):
    def __init__(self, lessons):
        self._lessons = lessons
        self.calls = []

    def get_lessons(self, start_date, end_date):
        self.calls.append((start_date, end_date))
        return self._lessons


class FakeMondayClient:
    def __init__(self, existing_ids=None, student_lookup=None, items=None, student_items=None, board_columns=None):
        self.existing_ids = set(existing_ids or [])
        self.student_lookup = dict(student_lookup or {})
        # items: list of {"item_id": ..., "item_name": ..., "columns": {col_id: text}},
        # served by get_items() for the Sessions board (the default/only board
        # in most tests). student_items: same shape, served for the Students
        # board specifically - only needed by tests that read both boards.
        self.items = list(items or [])
        self.student_items = list(student_items or [])
        # board_columns: list of {"id": ..., "title": ..., "type": ...}, used by get_board_columns()
        self.board_columns = list(board_columns or [])
        self.created_items = []
        self.connections = []
        self.student_updates = []
        self._next_item_id = 1000
        self.create_side_effect = None
        self.connect_side_effect = None
        # update_student_side_effect(item_id, column_values): raise to
        # simulate a failed write for one specific student.
        self.update_student_side_effect = None

    def get_existing_unique_ids(self, board_id, unique_id_column):
        return set(self.existing_ids)

    def get_student_lookup(self, board_id, teachworks_id_column):
        return dict(self.student_lookup)

    def create_session_item(self, board_id, group_id, item_name, column_values):
        if self.create_side_effect is not None:
            self.create_side_effect(column_values)
        item_id = str(self._next_item_id)
        self._next_item_id += 1
        self.created_items.append({
            "id": item_id,
            "board_id": board_id,
            "group_id": group_id,
            "item_name": item_name,
            "column_values": dict(column_values),
        })
        unique_key = column_values.get(config.COL_UNIQUE_ID)
        if unique_key:
            self.existing_ids.add(unique_key)
        return item_id

    def connect_student(self, board_id, item_id, column_id, student_item_id):
        if self.connect_side_effect is not None:
            self.connect_side_effect(item_id, student_item_id)
        self.connections.append((item_id, student_item_id))
        return item_id

    def get_items(self, board_id, column_ids):
        source = self.student_items if board_id == config.MONDAY_STUDENTS_BOARD_ID else self.items
        return [
            {
                "item_id": item["item_id"],
                "item_name": item.get("item_name", ""),
                "columns": {col: item["columns"].get(col, "") for col in column_ids},
            }
            for item in source
        ]

    def get_board_columns(self, board_id):
        return list(self.board_columns)

    def update_student_columns(self, board_id, item_id, column_values):
        if self.update_student_side_effect is not None:
            self.update_student_side_effect(item_id, column_values)
        self.student_updates.append({
            "board_id": board_id,
            "item_id": item_id,
            "column_values": dict(column_values),
        })
        return item_id


def make_lesson(lesson_id, session_date, participants, tutor="Jane Tutor", service="Math", location="Online", duration=60):
    return {
        "id": lesson_id,
        "from_date": session_date,
        "employee_name": tutor,
        "service_name": service,
        "location_name": location,
        "duration": duration,
        "participants": participants,
    }


def make_participant(student_id, student_name, attended=True, price=45.0):
    return {
        "student_id": student_id,
        "student_name": student_name,
        "attended": attended,
        "price": price,
    }
