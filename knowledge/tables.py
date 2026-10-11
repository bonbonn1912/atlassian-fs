"""Readable table serialization shared by Confluence and Jira parsers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TableCell:
    text: str
    header: bool = False
    colspan: int = 1
    rowspan: int = 1


def table_span(value: object) -> int:
    """Bound malformed/untrusted spans to avoid excessive grid expansion."""
    try:
        return max(1, min(int(str(value)), 100))
    except (TypeError, ValueError):
        return 1


def _cell_text(value: str) -> str:
    # Paragraphs stay within their cell, without introducing HTML into Markdown.
    return " / ".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


def _grid(rows: list[list[TableCell]]) -> list[list[TableCell | None]]:
    grid: list[list[TableCell | None]] = [[] for _ in rows]
    for y, row in enumerate(rows):
        x = 0
        for cell in row:
            # A cell cannot overlap a rowspan carried from a previous row.
            width = table_span(cell.colspan)
            while any(i < len(grid[y]) and grid[y][i] is not None
                      for i in range(x, x + width)):
                x += 1
            for target in grid[y:min(y + table_span(cell.rowspan), len(rows))]:
                target.extend([None] * max(0, x + width - len(target)))
                target[x:x + width] = [cell] * width
            x += width
    return grid


def render_table(rows: list[list[TableCell]], *, nested: bool = False) -> str:
    """Expand merged cells; retain empty columns and distinguish headers from data.

    Markdown has no merged cells: their content is repeated in the covered cells.
    Nested tables become labelled text inside the parent cell.
    """
    grid = _grid(rows)
    width = max((len(row) for row in grid), default=0)
    if not width:
        return ""
    grid = [row + [None] * (width - len(row)) for row in grid]
    header_count = 0
    for row in grid:
        if any(row) and all(cell is None or cell.header for cell in row):
            header_count += 1
        else:
            break
    headers = []
    for x in range(width):
        labels = []
        for row in grid[:header_count]:
            label = _cell_text(row[x].text) if row[x] else ""
            if label and (not labels or label != labels[-1]):
                labels.append(label)
        headers.append(" / ".join(labels) or f"Spalte {x + 1}")
    data = [[_cell_text(cell.text) if cell else "" for cell in row]
            for row in grid[header_count:]]
    if nested:
        return "\n".join("; ".join(f"{label}: {value}" for label, value in zip(headers, row))
                         for row in data) or "; ".join(headers)

    def markdown_row(values: list[str]) -> str:
        # Escape actual cell content, never structural separators.
        return "| " + " | ".join(v.replace("\\", "\\\\").replace("|", "\\|")
                                  .replace("<", "&lt;").replace(">", "&gt;")
                                  for v in values) + " |"

    return "\n".join([markdown_row(headers), "| " + " | ".join(["---"] * width) + " |"]
                     + [markdown_row(row) for row in data])


class StorageTable:
    """Collect a storage-format table while the main parser still extracts links."""

    def __init__(self) -> None:
        self.rows: list[list[TableCell]] = []
        self.row: list[TableCell] | None = None
        self.cell: TableCell | None = None
        self.parts: list[str] = []
        self.caption: list[str] = []

    def append(self, value: str) -> None:
        (self.parts if self.cell is not None else self.caption).append(value)

    def start_row(self) -> None:
        self.end_row()
        self.row = []

    def start_cell(self, header: bool, attrs: dict[str, str]) -> None:
        self.end_cell()
        if self.row is None:
            self.row = []
        self.cell = TableCell("", header, table_span(attrs.get("colspan", 1)),
                              table_span(attrs.get("rowspan", 1)))

    def end_cell(self) -> None:
        if self.cell is not None:
            self.cell.text = "".join(self.parts)
            assert self.row is not None
            self.row.append(self.cell)
            self.cell = None
            self.parts = []

    def end_row(self) -> None:
        self.end_cell()
        if self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def render(self, *, nested: bool = False) -> str:
        self.end_row()
        caption = _cell_text("".join(self.caption))
        body = render_table(self.rows, nested=nested)
        return "\n\n".join(part for part in (caption, body) if part)
