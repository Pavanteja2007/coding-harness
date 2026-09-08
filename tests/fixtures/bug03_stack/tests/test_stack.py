"""Tests for stacklib.stack."""
import pytest

from stacklib.stack import Stack, StackEmptyError


def test_push_pop():
    s = Stack()
    s.push(1)
    s.push(2)
    assert s.pop() == 2
    assert s.pop() == 1


def test_pop_empty_raises_stackemptyerror():
    # pop() on an empty stack must raise StackEmptyError, not IndexError.
    with pytest.raises(StackEmptyError):
        Stack().pop()


def test_peek_does_not_remove():
    s = Stack()
    s.push("a")
    assert s.peek() == "a"
    assert s.size() == 1


def test_peek_empty_raises():
    with pytest.raises(StackEmptyError):
        Stack().peek()


def test_size_and_is_empty():
    s = Stack()
    assert s.is_empty()
    s.push(None)
    assert not s.is_empty()
    assert s.size() == 1
