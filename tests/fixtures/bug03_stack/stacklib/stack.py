"""A simple LIFO stack for the stacklib package."""
from typing import Any, List


class StackEmptyError(Exception):
    """Raised when popping or peeking an empty stack."""


class Stack:
    """LIFO stack with push/pop/peek/size."""

    def __init__(self) -> None:
        self._items: List[Any] = []

    def push(self, item: Any) -> None:
        """Add `item` on top of the stack."""
        self._items.append(item)

    def pop(self) -> Any:
        """Remove and return the top item.

        Raises StackEmptyError if the stack is empty.
        """
        return self._items.pop()

    def peek(self) -> Any:
        """Return the top item without removing it.

        Raises StackEmptyError if the stack is empty.
        """
        if not self._items:
            raise StackEmptyError("peek from empty stack")
        return self._items[-1]

    def size(self) -> int:
        """Number of items in the stack."""
        return len(self._items)

    def is_empty(self) -> bool:
        """True if the stack has no items."""
        return not self._items
