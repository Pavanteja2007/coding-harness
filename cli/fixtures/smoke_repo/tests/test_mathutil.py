import mathutil


def test_mean():
    assert mathutil.mean([1, 2, 3, 4]) == 2.5


def test_median():
    assert mathutil.median([3, 1, 2]) == 2
    assert mathutil.median([4, 3, 1, 2]) == 2.5
